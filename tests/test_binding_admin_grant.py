import unittest

from fastapi_service.services import binding_service
from shared_lib.config import settings
from shared_lib.models import Player, UserBinding
from tortoise import Tortoise

TORTOISE_TEST_CONFIG = {
    "connections": {"default": "sqlite://:memory:"},
    "apps": {
        "models": {
            "models": ["shared_lib.models"],
            "default_connection": "default",
        }
    },
    "use_tz": False,
    "timezone": "Asia/Shanghai",
}


class BindingAdminGrantTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await Tortoise.init(config=TORTOISE_TEST_CONFIG)
        await Tortoise.generate_schemas()
        self.original_super_admin_platform_uids = settings.super_admin_platform_uids.copy()

    async def asyncTearDown(self) -> None:
        settings.super_admin_platform_uids = self.original_super_admin_platform_uids
        await Tortoise.close_connections()

    async def test_grant_admin_by_platform_marks_bound_player_admin(self) -> None:
        player = await Player.create(nucleus_id=1001, name="bound-player")
        await UserBinding.create(platform="qq", platform_uid="2001", player=player, app_key="app-key-2001")

        data, err = await binding_service.grant_admin_by_platform("qq", "2001")

        self.assertIsNone(err)
        self.assertEqual(data["status"], "granted")
        await player.refresh_from_db()
        self.assertTrue(player.is_admin)

        data, err = await binding_service.grant_admin_by_platform("qq", "2001")

        self.assertIsNone(err)
        self.assertEqual(data["status"], "already_admin")

    async def test_grant_admin_by_platform_skips_super_admin_binding(self) -> None:
        settings.super_admin_platform_uids = [1259332131, 2002]
        player = await Player.create(nucleus_id=1002, name="super-admin-bound")
        await UserBinding.create(platform="qq", platform_uid="2002", player=player, app_key="app-key-super")

        data, err = await binding_service.grant_admin_by_platform("qq", "2002")

        self.assertIsNone(err)
        self.assertEqual(data["status"], "skipped_super_admin")
        await player.refresh_from_db()
        self.assertFalse(player.is_admin)

    async def test_bind_player_reports_configured_super_admin(self) -> None:
        settings.super_admin_platform_uids = [1259332131, 2003]
        await Player.create(nucleus_id=1003, name="configured-super-admin")

        data, err = await binding_service.bind_player("qq", "2003", "1003")

        self.assertIsNone(err)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertTrue(data["is_admin"])
        self.assertTrue(data["is_super_admin"])

    async def test_grant_admin_by_platform_returns_not_bound_for_unbound_qq(self) -> None:
        data, err = await binding_service.grant_admin_by_platform("qq", "9999")

        self.assertIsNone(err)
        self.assertEqual(data["status"], "not_bound")
        self.assertEqual(data["platform_uid"], "9999")


if __name__ == "__main__":
    unittest.main()
