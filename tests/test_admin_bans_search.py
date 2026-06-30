import unittest
from datetime import datetime

from fastapi_service.api.v1 import admin as admin_api
from fastapi_service.core.errors import ErrorCode
from fastapi_service.services import admin_management_service, admin_service, player_access_service
from shared_lib.models import Player, PlayerAccessNotice, PlayerAccessOperation, PlayerAccessRule, UserBinding
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
    LEGACY_EXECUTION_RESULT_KEYS = {
        "broadcast_total",
        "broadcast_success_count",
        "hit_server",
        "hit_servers",
    }

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

    async def test_bans_ignores_player_status_without_access_operation(self) -> None:
        await Player.create(nucleus_id=1009800111070, name="JP_Player", status="banned")

        rows, total = await admin_service.list_bans(
            page_size=20,
            offset=0,
            is_admin=True,
            player_name="jp_player",
        )

        self.assertEqual(total, 0)
        self.assertEqual(rows, [])

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
        assert err is not None
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
        assert data is not None
        self.assertTrue(data["self_unban"])
        self.assertFalse(notice.requires_ack)
        self.assertIsNotNone(notice.acknowledged_at)
        self.assertEqual(player.status, "offline")

        rows, total = await admin_service.list_bans(page_size=20, offset=0)

        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["resolution_status"], "resolved")
        self.assertNotIn("resolution_label", rows[0])
        self.assertEqual(rows[0]["resolved_at"], notice.acknowledged_at)

    async def test_bans_can_filter_acknowledged_notice_rows(self) -> None:
        pending_player = await Player.create(nucleus_id=1009800111080, name="Pending_Notice", status="kicked")
        confirmed_player = await Player.create(nucleus_id=1009800111081, name="Confirmed_Notice", status="offline")
        pending_operation = await PlayerAccessOperation.create(
            action="kick",
            target_type="player",
            target_value=str(pending_player.nucleus_id),
            normalized_target=str(pending_player.nucleus_id),
            server_scope="global",
            reason="RULES",
            operator="unit-test",
            player=pending_player,
        )
        confirmed_operation = await PlayerAccessOperation.create(
            action="kick",
            target_type="player",
            target_value=str(confirmed_player.nucleus_id),
            normalized_target=str(confirmed_player.nucleus_id),
            server_scope="global",
            reason="RULES",
            operator="unit-test",
            player=confirmed_player,
        )
        confirmed_at = datetime.now()
        await PlayerAccessNotice.create(
            player=pending_player,
            uid=str(pending_player.nucleus_id),
            action="kick",
            reason="RULES",
            requires_ack=True,
            operation=pending_operation,
        )
        await PlayerAccessNotice.create(
            player=confirmed_player,
            uid=str(confirmed_player.nucleus_id),
            action="kick",
            reason="RULES",
            requires_ack=False,
            acknowledged_at=confirmed_at,
            operation=confirmed_operation,
        )

        all_rows, all_total = await admin_service.list_bans(page_size=20, offset=0)
        pending_rows, pending_total = await admin_service.list_bans(page_size=20, offset=0, acknowledged=False)
        confirmed_rows, confirmed_total = await admin_service.list_bans(page_size=20, offset=0, acknowledged=True)

        self.assertEqual(all_total, 2)
        self.assertEqual(pending_total, 1)
        self.assertEqual(pending_rows[0]["player"]["name"], "Pending_Notice")
        self.assertEqual(confirmed_total, 1)
        self.assertEqual(confirmed_rows[0]["player"]["name"], "Confirmed_Notice")
        self.assertEqual({row["player"]["name"] for row in all_rows}, {"Pending_Notice", "Confirmed_Notice"})

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
        self.assertNotIn("resolution_label", rows[0])
        self.assertEqual(rows[0]["resolved_at"], unban_operation.created_at)

    async def test_admin_actions_use_sdk_access_execution_without_legacy_result_fields(self) -> None:
        kick_player = await Player.create(nucleus_id=1009800111074, name="Sdk_Kick_Player")
        ban_player = await Player.create(nucleus_id=1009800111075, name="Sdk_Ban_Player")

        kick_data, kick_err = await admin_management_service.apply_access_action(
            action="kick",
            target_type="player",
            target_value=kick_player.nucleus_id,
            reason="RULES",
            operator_name="unit-test",
        )
        ban_data, ban_err = await admin_management_service.apply_access_action(
            action="ban",
            target_type="player",
            target_value=ban_player.nucleus_id,
            reason="CHEAT",
            operator_name="unit-test",
        )
        unban_data, unban_err = await admin_management_service.apply_access_action(
            action="unban",
            target_type="player",
            target_value=ban_player.nucleus_id,
            reason="RULES",
            operator_name="unit-test",
        )

        for data, err in ((kick_data, kick_err), (ban_data, ban_err), (unban_data, unban_err)):
            self.assertIsNone(err)
            assert data is not None
            self.assertEqual(data["execution_mode"], "sdk_access")
            self.assertFalse(self.LEGACY_EXECUTION_RESULT_KEYS & data.keys())
            result = data["operation"]["result"] or {}
            self.assertEqual(result["execution_mode"], "sdk_access")
            self.assertFalse(self.LEGACY_EXECUTION_RESULT_KEYS & result.keys())

        assert kick_data is not None
        self.assertIsNotNone(kick_data["notice"])

        assert unban_data is not None
        self.assertGreaterEqual(len(unban_data["released_rules"]), 1)

    async def test_admin_action_prefers_address_server_key_over_legacy_server_id(self) -> None:
        player = await Player.create(nucleus_id=1009800111076, name="Scoped_Kick_Player")

        data, err = await admin_management_service.apply_access_action(
            action="kick",
            target_type="player",
            target_value=player.nucleus_id,
            reason="RULES",
            operator_name="unit-test",
            server_scope="server",
            server_id="legacy-config-id",
            server_key="1.2.3.4:37015",
        )

        self.assertIsNone(err)
        assert data is not None
        self.assertEqual(data["server_id"], "1.2.3.4:37015")
        self.assertEqual(data["operation"]["server_id"], "1.2.3.4:37015")
        self.assertEqual(data["notice"]["server_id"], "1.2.3.4:37015")

    async def test_admin_player_list_uses_id_order_and_bulk_fields(self) -> None:
        old_player = await Player.create(nucleus_id=1009800111090, name="Old_List_Player")
        new_player = await Player.create(nucleus_id=1009800111091, name="New_List_Player")
        await UserBinding.create(platform="qq", platform_uid="20001", player=new_player, app_key="app-key-list-new")
        await PlayerAccessRule.create(
            rule_type="uid",
            action="deny",
            value=str(new_player.nucleus_id),
            reason="RULES",
            rule_id="ban:uid:list-new",
            source_action="ban",
            player=new_player,
        )

        rows, total = await admin_management_service.list_players(page_size=10, offset=0)

        self.assertEqual(total, 2)
        self.assertEqual([row["id"] for row in rows], [new_player.id, old_player.id])
        self.assertEqual(rows[0]["qq"], "20001")
        self.assertFalse(rows[0]["access"]["allow"])
        self.assertEqual(rows[0]["access"]["rule_id"], "ban:uid:list-new")
        self.assertEqual(rows[0]["display_status"], "ban")

    async def test_access_rule_list_includes_player_summary_from_uid_value(self) -> None:
        player = await Player.create(
            nucleus_id=1009800111092,
            name="Rule_Summary_Player",
            kick_count=1,
            ban_count=2,
            status="offline",
            country="Japan",
            input_device="keyboard_mouse",
        )
        await PlayerAccessRule.create(
            rule_type="uid",
            action="deny",
            value=str(player.nucleus_id),
            reason="RULES",
            rule_id="ban:uid:summary-player",
        )

        rows, total = await player_access_service.list_access_rules(page_size=10, offset=0)

        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["player"]["id"], player.id)
        self.assertEqual(rows[0]["player"]["name"], "Rule_Summary_Player")
        self.assertEqual(rows[0]["player"]["nucleus_id"], player.nucleus_id)
        self.assertEqual(rows[0]["player"]["kick_count"], 1)
        self.assertEqual(rows[0]["player"]["ban_count"], 2)
        self.assertEqual(rows[0]["player"]["status"], "offline")
        self.assertEqual(rows[0]["player"]["country"], "Japan")
        self.assertEqual(rows[0]["player"]["input_device"], "keyboard_mouse")

    async def test_access_operation_list_includes_player_summary_from_uid_target(self) -> None:
        player = await Player.create(
            nucleus_id=1009800111093,
            name="Operation_Summary_Player",
            kick_count=3,
            ban_count=4,
            status="banned",
            country="United States",
            input_device="controller",
        )
        await PlayerAccessOperation.create(
            action="ban",
            target_type="player",
            target_value=str(player.nucleus_id),
            normalized_target=str(player.nucleus_id),
            server_scope="global",
            reason="CHEAT",
            operator="unit-test",
        )

        rows, total = await player_access_service.list_access_operations(page_size=10, offset=0)

        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["player"]["id"], player.id)
        self.assertEqual(rows[0]["player"]["name"], "Operation_Summary_Player")
        self.assertEqual(rows[0]["player"]["nucleus_id"], player.nucleus_id)
        self.assertEqual(rows[0]["player"]["kick_count"], 3)
        self.assertEqual(rows[0]["player"]["ban_count"], 4)
        self.assertEqual(rows[0]["player"]["status"], "banned")
        self.assertEqual(rows[0]["player"]["country"], "United States")
        self.assertEqual(rows[0]["player"]["input_device"], "controller")

    async def test_second_pending_kick_escalates_to_uid_ban_without_ip_rule(self) -> None:
        player = await Player.create(nucleus_id=1009800111077, name="Pending_Kick_Player", ip="203.0.113.77")

        first_data, first_err = await admin_management_service.apply_access_action(
            action="kick",
            target_type="player",
            target_value=player.nucleus_id,
            reason="RULES",
            operator_name="unit-test",
        )
        second_data, second_err = await admin_management_service.apply_access_action(
            action="kick",
            target_type="player",
            target_value=player.nucleus_id,
            reason="NO_COVER",
            operator_name="unit-test",
        )

        self.assertIsNone(first_err)
        self.assertIsNone(second_err)
        assert first_data is not None
        assert second_data is not None
        self.assertEqual(await PlayerAccessOperation.filter(player=player, action="kick").count(), 1)
        self.assertEqual(await PlayerAccessOperation.filter(player=player, action="ban").count(), 1)
        self.assertEqual(await PlayerAccessNotice.filter(player=player, action="kick").count(), 1)
        self.assertEqual(await PlayerAccessNotice.filter(player=player, action="kick", requires_ack=True, acknowledged_at__isnull=True).count(), 0)
        self.assertEqual(second_data["operation"]["action"], "ban")
        self.assertEqual(second_data["operation"]["reason"], "NO_COVER")
        self.assertTrue(second_data["action_escalated"])
        self.assertEqual(second_data["escalated_from_action"], "kick")
        self.assertEqual(second_data["escalated_to_action"], "ban")
        self.assertEqual(second_data["previous_notice_id"], first_data["notice"]["id"])
        self.assertIn(first_data["notice"]["id"], second_data["superseded_notice_ids"])
        self.assertFalse(second_data["sync_player_ip"])
        self.assertFalse(second_data["ip_synced"])

        uid_rule = await PlayerAccessRule.get(rule_type="uid", value=str(player.nucleus_id), source_action="ban")
        self.assertEqual(uid_rule.reason, "NO_COVER")
        self.assertEqual(await PlayerAccessRule.filter(rule_type="ip", source_action="ban", player=player).count(), 0)
        await player.refresh_from_db()
        self.assertEqual(player.kick_count, 1)
        self.assertEqual(player.ban_count, 1)
        self.assertEqual(player.status, "banned")

    async def test_pending_ban_action_is_overwritten_and_syncs_ip_without_duplicate_operation(self) -> None:
        player = await Player.create(nucleus_id=1009800111088, name="Pending_Ban_Player", ip="203.0.113.88")

        first_data, first_err = await admin_management_service.apply_access_action(
            action="ban",
            target_type="player",
            target_value=player.nucleus_id,
            reason="RULES",
            operator_name="unit-test",
        )
        second_data, second_err = await admin_management_service.apply_access_action(
            action="ban",
            target_type="player",
            target_value=player.nucleus_id,
            reason="CHEAT",
            operator_name="unit-test",
        )

        self.assertIsNone(first_err)
        self.assertIsNone(second_err)
        assert first_data is not None
        assert second_data is not None
        self.assertEqual(await PlayerAccessOperation.filter(player=player, action="ban").count(), 1)
        self.assertEqual(second_data["operation"]["id"], first_data["operation"]["id"])
        self.assertEqual(second_data["operation"]["reason"], "CHEAT")
        self.assertTrue(second_data["operation_reused"])
        self.assertTrue(second_data["sync_player_ip"])
        self.assertTrue(second_data["ip_synced"])

        uid_rule = await PlayerAccessRule.get(rule_type="uid", value=str(player.nucleus_id), source_action="ban")
        ip_rule = await PlayerAccessRule.get(rule_type="ip", source_action="ban")
        self.assertEqual(uid_rule.reason, "CHEAT")
        self.assertEqual(ip_rule.value, "203.0.113.88")
        self.assertEqual(ip_rule.reason, "CHEAT")
        self.assertEqual(getattr(ip_rule, "source_operation_id", None), second_data["operation"]["id"])

        await player.refresh_from_db()
        self.assertEqual(player.ban_count, 1)
        self.assertEqual(player.status, "banned")

    async def test_bans_suppresses_duplicate_kick_rows_when_pending_notice_exists(self) -> None:
        player = await Player.create(nucleus_id=1009800111078, name="Duplicate_Kick_Player", kick_count=2)
        old_operation = await PlayerAccessOperation.create(
            action="kick",
            target_type="player",
            target_value=str(player.nucleus_id),
            normalized_target=str(player.nucleus_id),
            server_scope="global",
            reason="RULES",
            operator="unit-test",
            player=player,
        )
        pending_operation = await PlayerAccessOperation.create(
            action="kick",
            target_type="player",
            target_value=str(player.nucleus_id),
            normalized_target=str(player.nucleus_id),
            server_scope="global",
            reason="NO_COVER",
            operator="unit-test",
            player=player,
        )
        await PlayerAccessNotice.create(
            player=player,
            uid=str(player.nucleus_id),
            action="kick",
            reason="NO_COVER",
            operation=pending_operation,
            requires_ack=True,
        )

        rows, total = await admin_service.list_bans(page_size=20, offset=0)

        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["operation_id"], pending_operation.id)
        self.assertNotEqual(rows[0]["operation_id"], old_operation.id)
        self.assertEqual(rows[0]["resolution_status"], "pending")

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
