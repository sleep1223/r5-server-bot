import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi_service.api.v1 import admin_management
from fastapi_service.services import binding_role_service, binding_service
from shared_lib.config import settings
from shared_lib.models import Player, UserBinding
from tortoise import Tortoise

TORTOISE_TEST_CONFIG = {
    "connections": {"default": "sqlite://:memory:"},
    "apps": {"models": {"models": ["shared_lib.models"], "default_connection": "default"}},
    "use_tz": False,
    "timezone": "Asia/Shanghai",
}


class BindingRoleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await Tortoise.init(config=TORTOISE_TEST_CONFIG)
        await Tortoise.generate_schemas()
        self.original_admin_qqs = settings.configured_admin_qqs.copy()
        self.original_super_admin_qqs = settings.configured_super_admin_qqs.copy()
        self.original_excluded_qqs = settings.milky_admin_group_grant_excluded_qqs.copy()
        settings.configured_admin_qqs = []
        settings.configured_super_admin_qqs = []
        settings.milky_admin_group_grant_excluded_qqs = []

    async def asyncTearDown(self) -> None:
        settings.configured_admin_qqs = self.original_admin_qqs
        settings.configured_super_admin_qqs = self.original_super_admin_qqs
        settings.milky_admin_group_grant_excluded_qqs = self.original_excluded_qqs
        await Tortoise.close_connections()

    async def test_bind_player_applies_configured_roles(self) -> None:
        settings.configured_admin_qqs = [2001]
        settings.configured_super_admin_qqs = [2002]
        await Player.create(nucleus_id=1001, name="admin-player")
        await Player.create(nucleus_id=1002, name="super-player")

        admin_data, admin_err = await binding_service.bind_player("qq", "2001", "1001")
        super_data, super_err = await binding_service.bind_player("qq", "2002", "1002")

        self.assertIsNone(admin_err)
        self.assertIsNone(super_err)
        assert admin_data is not None and super_data is not None
        self.assertTrue(admin_data["is_admin"])
        self.assertFalse(admin_data["is_super_admin"])
        self.assertTrue(super_data["is_admin"])
        self.assertTrue(super_data["is_super_admin"])

    async def test_apply_configured_roles_only_grants(self) -> None:
        player = await Player.create(nucleus_id=1003, name="configured-player")
        binding = await UserBinding.create(platform="qq", platform_uid="2003", player=player, app_key="app-key-2003")
        settings.configured_super_admin_qqs = [2003]

        summary = await binding_role_service.apply_configured_roles()

        await binding.refresh_from_db()
        self.assertEqual(summary["super_admin"], 1)
        self.assertTrue(binding.is_admin)
        self.assertTrue(binding.is_super_admin)

        settings.configured_super_admin_qqs = []
        await binding_role_service.apply_configured_roles()
        await binding.refresh_from_db()
        self.assertTrue(binding.is_super_admin)

    async def test_group_sync_grants_bound_members_and_honors_exclusions(self) -> None:
        player1 = await Player.create(nucleus_id=1004, name="group-player")
        player2 = await Player.create(nucleus_id=1005, name="excluded-player")
        binding1 = await UserBinding.create(platform="qq", platform_uid="2004", player=player1, app_key="app-key-2004")
        binding2 = await UserBinding.create(platform="qq", platform_uid="2005", player=player2, app_key="app-key-2005")
        settings.milky_admin_group_grant_excluded_qqs = [2005]

        summary = await binding_role_service.grant_admins_by_qqs({"2004", "2005", "9999"})

        await binding1.refresh_from_db()
        await binding2.refresh_from_db()
        self.assertTrue(binding1.is_admin)
        self.assertFalse(binding2.is_admin)
        self.assertEqual(summary["granted"], 1)
        self.assertEqual(summary["excluded"], 1)

    async def test_cannot_demote_last_super_admin(self) -> None:
        player = await Player.create(nucleus_id=1006, name="last-super")
        binding = await UserBinding.create(platform="qq", platform_uid="2006", player=player, app_key="app-key-2006", is_admin=True, is_super_admin=True)

        data, err = await binding_role_service.set_binding_role(binding_id=binding.id, role="user", operator=binding)

        self.assertIsNone(data)
        self.assertEqual(err, "不能降级最后一个超级管理员")

    async def test_bot_kick_uses_binding_admin_role_and_ban_requires_super_admin(self) -> None:
        player = await Player.create(nucleus_id=1007, name="bot-operator")
        binding = await UserBinding.create(platform="qq", platform_uid="2007", player=player, app_key="app-key-2007", is_admin=True)
        body = admin_management.BotAccessActionBody(operator_uid="2007", target_type="player", target_value="target-player", reason="RULES")

        with patch.object(admin_management.admin_management_service, "apply_access_action", new=AsyncMock(return_value=({"operation": {"action": "kick"}}, None))) as apply_action:
            result = await admin_management.admin_bot_apply_access_action("kick", body)

        self.assertEqual(result["code"], "0000")
        apply_action.assert_awaited_once()

        with self.assertRaises(HTTPException) as context:
            await admin_management.admin_bot_apply_access_action("ban", body)
        self.assertEqual(context.exception.status_code, 403)

        binding.is_super_admin = True
        await binding.save(update_fields=["is_super_admin"])
        with patch.object(admin_management.admin_management_service, "apply_access_action", new=AsyncMock(return_value=({"operation": {"action": "ban"}}, None))) as apply_action:
            result = await admin_management.admin_bot_apply_access_action("ban", body)

        self.assertEqual(result["code"], "0000")
        apply_action.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
