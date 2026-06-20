import unittest

from fastapi_service.api.v1 import admin as admin_api
from fastapi_service.core.errors import ErrorCode
from fastapi_service.services import admin_service
from shared_lib.models import BanRecord, Player, PlayerAccessNotice, PlayerAccessOperation
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


class AdminBansSearchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        admin_api._SELF_UNBAN_IP_LAST_SUCCESS.clear()
        await Tortoise.init(config=TORTOISE_TEST_CONFIG)
        await Tortoise.generate_schemas()

    async def asyncTearDown(self) -> None:
        admin_api._SELF_UNBAN_IP_LAST_SUCCESS.clear()
        await Tortoise.close_connections()

    async def test_bans_exact_player_name_search_is_case_insensitive_for_operation_targets(self) -> None:
        await Player.create(nucleus_id=1009800111069, name="CN_Aeroese", status="banned")
        await PlayerAccessOperation.create(
            action="ban",
            target_type="player",
            target_value="CN_Aeroese",
            normalized_target="CN_Aeroese",
            server_scope="global",
            reason="RULES",
            operator="unit-test",
        )

        rows, total = await admin_service.list_bans(
            page_size=20,
            offset=0,
            is_admin=True,
            player_query="cn_aeroese",
        )

        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["source"], "access_operation")
        self.assertEqual(rows[0]["target_value"], "CN_Aeroese")

    async def test_bans_exact_player_name_search_is_case_insensitive_for_legacy_bans(self) -> None:
        player = await Player.create(nucleus_id=1009800111070, name="JP_Player", status="banned")
        await BanRecord.create(player=player, reason="CHEAT", operator="unit-test")

        rows, total = await admin_service.list_bans(
            page_size=20,
            offset=0,
            is_admin=True,
            player_name="jp_player",
        )

        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["source"], "ban_record")
        self.assertEqual(rows[0]["player"]["name"], "JP_Player")

    async def test_only_pending_kick_notice_can_self_unban(self) -> None:
        player = await Player.create(nucleus_id=1009800111071, name="Kick_Player", status="kicked")
        operation = await PlayerAccessOperation.create(
            action="kick",
            target_type="player",
            target_value=str(player.nucleus_id),
            normalized_target=str(player.nucleus_id),
            server_scope="global",
            reason="RULES",
            operator="unit-test",
            player=player,
        )
        await PlayerAccessNotice.create(
            player=player,
            uid=str(player.nucleus_id),
            action="ban",
            reason="RULES",
            requires_ack=True,
            operation=operation,
        )

        rows, total = await admin_service.list_bans(page_size=20, offset=0)
        data, err = await admin_service.self_unban_player(
            nucleus_id=player.nucleus_id,
            operation_id=operation.id,
            confirmation_text=admin_service.SELF_UNBAN_CONFIRMATION_TEXT,
        )

        self.assertEqual(total, 1)
        self.assertFalse(rows[0]["can_self_unban"])
        self.assertEqual(rows[0]["resolution_status"], "active")
        self.assertIsNone(data)
        self.assertEqual(err["code"], ErrorCode.INVALID_REASON)

    async def test_self_unban_acknowledges_pending_kick_notice(self) -> None:
        player = await Player.create(nucleus_id=1009800111072, name="Real_Kick_Player", status="kicked")
        operation = await PlayerAccessOperation.create(
            action="kick",
            target_type="player",
            target_value=str(player.nucleus_id),
            normalized_target=str(player.nucleus_id),
            server_scope="global",
            reason="RULES",
            operator="unit-test",
            player=player,
        )
        notice = await PlayerAccessNotice.create(
            player=player,
            uid=str(player.nucleus_id),
            action="kick",
            reason="RULES",
            requires_ack=True,
            operation=operation,
        )

        data, err = await admin_service.self_unban_player(
            nucleus_id=player.nucleus_id,
            operation_id=operation.id,
            confirmation_text=admin_service.SELF_UNBAN_CONFIRMATION_TEXT,
        )

        await notice.refresh_from_db()
        await player.refresh_from_db()
        self.assertIsNone(err)
        self.assertTrue(data["self_unban"])
        self.assertFalse(notice.requires_ack)
        self.assertIsNotNone(notice.acknowledged_at)
        self.assertEqual(player.status, "offline")

        rows, total = await admin_service.list_bans(page_size=20, offset=0)

        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["resolution_status"], "resolved")
        self.assertEqual(rows[0]["resolution_label"], "已解除")
        self.assertEqual(rows[0]["resolved_at"], notice.acknowledged_at)

    async def test_unban_operation_marks_ban_row_resolved(self) -> None:
        player = await Player.create(nucleus_id=1009800111073, name="Unbanned_Player", status="offline")
        await PlayerAccessOperation.create(
            action="ban",
            target_type="player",
            target_value=str(player.nucleus_id),
            normalized_target=str(player.nucleus_id),
            server_scope="global",
            reason="CHEAT",
            operator="unit-test",
            player=player,
        )
        unban_operation = await PlayerAccessOperation.create(
            action="unban",
            target_type="player",
            target_value=str(player.nucleus_id),
            normalized_target=str(player.nucleus_id),
            server_scope="global",
            operator="unit-test",
            player=player,
        )

        rows, total = await admin_service.list_bans(page_size=20, offset=0)

        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["resolution_status"], "resolved")
        self.assertEqual(rows[0]["resolution_label"], "已解除")
        self.assertEqual(rows[0]["resolved_at"], unban_operation.created_at)

    async def test_self_unban_ip_rate_limit_reserves_and_releases(self) -> None:
        reserved_at, retry_after = admin_api._reserve_self_unban_ip_slot("203.0.113.10", now=100.0)
        blocked_reserved_at, blocked_retry_after = admin_api._reserve_self_unban_ip_slot("203.0.113.10", now=120.0)

        self.assertEqual(reserved_at, 100.0)
        self.assertEqual(retry_after, 0)
        self.assertIsNone(blocked_reserved_at)
        self.assertEqual(blocked_retry_after, 40)

        admin_api._release_self_unban_ip_slot("203.0.113.10", reserved_at)
        reserved_after_release, retry_after_release = admin_api._reserve_self_unban_ip_slot("203.0.113.10", now=120.0)

        self.assertEqual(reserved_after_release, 120.0)
        self.assertEqual(retry_after_release, 0)


if __name__ == "__main__":
    unittest.main()
