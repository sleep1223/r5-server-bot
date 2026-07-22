import unittest
from datetime import datetime

from fastapi_service.api.v1 import admin as admin_api
from fastapi_service.core.errors import ErrorCode
from fastapi_service.services import admin_management_service, admin_service, player_access_service, server_service
from shared_lib.models import Player, PlayerAccessNotice, PlayerAccessOperation, PlayerAccessRule, Server, UserBinding
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

    async def test_whitelisted_player_supports_full_access_action_lifecycle(self) -> None:
        player = await Player.create(nucleus_id=1021977259236, name="Whitelisted_Player")
        allow_rule = await PlayerAccessRule.create(
            rule_type="uid",
            action="allow",
            value=str(player.nucleus_id),
            server_scope="global",
            rule_id="allow:uid:1021977259236",
            priority=100,
            player=player,
        )

        initial_access = await player_access_service.get_player_access_state(player=player)
        self.assertTrue(initial_access["allow"])
        self.assertEqual(initial_access["rule_id"], allow_rule.rule_id)

        kick_data, kick_err = await admin_management_service.apply_access_action(
            action="kick",
            target_type="player",
            target_value=player.nucleus_id,
            reason="RULES",
            operator_name="unit-test",
        )
        self.assertIsNone(kick_err)
        assert kick_data is not None
        kicked_access = await player_access_service.get_player_access_state(player=player)
        self.assertFalse(kicked_access["allow"])
        self.assertEqual(player_access_service.action_from_access_decision(kicked_access), "kick")

        self_unban_data, self_unban_err = await admin_service.self_unban_player(
            nucleus_id=player.nucleus_id,
            operation_id=kick_data["operation"]["id"],
            confirmation_text=admin_service.SELF_UNBAN_CONFIRMATION_TEXT,
        )
        self.assertIsNone(self_unban_err)
        assert self_unban_data is not None
        self.assertTrue(self_unban_data["self_unban"])
        access_after_self_unban = await player_access_service.get_player_access_state(player=player)
        self.assertTrue(access_after_self_unban["allow"])
        self.assertEqual(access_after_self_unban["rule_id"], allow_rule.rule_id)

        ban_data, ban_err = await admin_management_service.apply_access_action(
            action="ban",
            target_type="player",
            target_value=player.nucleus_id,
            reason="CHEAT",
            operator_name="unit-test",
        )
        self.assertIsNone(ban_err)
        assert ban_data is not None
        banned_access = await player_access_service.get_player_access_state(player=player)
        self.assertFalse(banned_access["allow"])
        self.assertEqual(player_access_service.action_from_access_decision(banned_access), "ban")
        self.assertEqual(banned_access["rule_id"], f"ban:uid:{player.nucleus_id}")

        access_trace = await player_access_service.trace_player_access(uid=player.nucleus_id)
        self.assertEqual(access_trace["checks"][1]["step"], "uid_admin_ban")
        self.assertTrue(access_trace["checks"][1]["matched"])

        rows, total = await admin_management_service.list_players(page_size=10, offset=0)
        self.assertEqual(total, 1)
        self.assertFalse(rows[0]["access"]["allow"])
        self.assertEqual(rows[0]["display_status"], "ban")

        unban_data, unban_err = await admin_management_service.apply_access_action(
            action="unban",
            target_type="player",
            target_value=player.nucleus_id,
            reason="RULES",
            operator_name="unit-test",
        )
        self.assertIsNone(unban_err)
        assert unban_data is not None
        self.assertNotIn(allow_rule.rule_id, {rule["rule_id"] for rule in unban_data["released_rules"]})

        await allow_rule.refresh_from_db()
        self.assertTrue(allow_rule.enabled)
        access_after_unban = await player_access_service.get_player_access_state(player=player)
        self.assertTrue(access_after_unban["allow"])
        self.assertEqual(access_after_unban["rule_id"], allow_rule.rule_id)

    async def test_admin_action_prefers_address_server_key_over_legacy_server_id(self) -> None:
        player = await Player.create(nucleus_id=1009800111076, name="Scoped_Kick_Player")
        server = await Server.create(
            server_id="legacy-config-id",
            host="1.2.3.4",
            port=37015,
            name="Scoped Test Server",
        )

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
        self.assertEqual(data["server_db_id"], server.id)
        self.assertEqual(data["operation"]["server_db_id"], server.id)
        self.assertEqual(data["notice"]["server_db_id"], server.id)

    async def test_admin_server_options_only_include_self_hosted_servers(self) -> None:
        visible_without_short_name = await Server.create(
            server_id="visible-server",
            host="10.0.0.1",
            port=37015,
            name="Alpha Self Hosted Server",
            is_self_hosted=True,
        )
        visible_with_short_name = await Server.create(
            server_id="visible-short-name-server",
            host="10.0.0.2",
            port=37015,
            name="Beta Self Hosted Server",
            short_name="短名服",
            is_self_hosted=True,
        )
        await Server.create(
            server_id="hidden-non-self-hosted",
            host="10.0.0.3",
            port=37015,
            name="Hidden Test Server",
            short_name="隐藏服",
            is_self_hosted=False,
        )
        await Server.create(
            server_id="hidden-empty-short-name",
            host="10.0.0.4",
            port=37015,
            name="Empty Short Name Server",
            short_name="",
            is_self_hosted=False,
        )

        rows = await server_service.list_admin_server_options()
        hidden_rows = await server_service.list_admin_server_options(q="Hidden")

        self.assertEqual([row["id"] for row in rows], [visible_without_short_name.id, visible_with_short_name.id])
        self.assertEqual(rows[0]["label"], "Alpha Self Hosted Server")
        self.assertEqual(hidden_rows, [])

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
        self.assertEqual(rows[0]["bindings"][0]["platform_uid"], "20001")
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

    async def test_second_pending_kick_updates_reason_without_escalating(self) -> None:
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
        self.assertEqual(await PlayerAccessOperation.filter(player=player, action="ban").count(), 0)
        self.assertEqual(await PlayerAccessNotice.filter(player=player, action="kick").count(), 1)
        self.assertEqual(await PlayerAccessNotice.filter(player=player, action="kick", requires_ack=True, acknowledged_at__isnull=True).count(), 1)
        self.assertEqual(second_data["operation"]["id"], first_data["operation"]["id"])
        self.assertEqual(second_data["operation"]["action"], "kick")
        self.assertEqual(second_data["operation"]["reason"], "NO_COVER")
        self.assertEqual(second_data["notice"]["id"], first_data["notice"]["id"])
        self.assertEqual(second_data["notice"]["reason"], "NO_COVER")
        self.assertFalse(second_data["action_escalated"])
        self.assertTrue(second_data["pending_notice_reused"])
        self.assertTrue(second_data["reason_updated"])
        self.assertEqual(second_data["previous_reason"], "RULES")

        self.assertFalse(await PlayerAccessRule.filter(rule_type="uid", value=str(player.nucleus_id), source_action="ban").exists())
        self.assertEqual(await PlayerAccessRule.filter(rule_type="ip", source_action="ban", player=player).count(), 0)
        await player.refresh_from_db()
        self.assertEqual(player.kick_count, 1)
        self.assertEqual(player.ban_count, 0)
        self.assertEqual(player.status, "offline")

    async def test_second_pending_kick_same_reason_returns_existing_record_error(self) -> None:
        player = await Player.create(nucleus_id=1009800111081, name="Pending_Kick_Same_Reason", ip="203.0.113.81")

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
            reason="RULES",
            operator_name="unit-test",
        )

        self.assertIsNone(first_err)
        assert first_data is not None
        self.assertIsNone(second_data)
        self.assertIsNotNone(second_err)
        assert second_err is not None
        self.assertEqual(second_err["code"], ErrorCode.INVALID_REASON)
        self.assertIn("已有未确认 kick 记录", second_err["msg"])
        self.assertEqual(second_err["data"]["notice"]["id"], first_data["notice"]["id"])
        self.assertEqual(await PlayerAccessOperation.filter(player=player, action="kick").count(), 1)
        self.assertEqual(await PlayerAccessOperation.filter(player=player, action="ban").count(), 0)
        notice = await PlayerAccessNotice.get(player=player, action="kick")
        self.assertEqual(notice.reason, "RULES")
        self.assertTrue(notice.requires_ack)
        self.assertIsNone(notice.acknowledged_at)

    async def test_acknowledged_kick_notice_escalates_to_uid_ban_without_ip_rule(self) -> None:
        player = await Player.create(nucleus_id=1009800111079, name="Acknowledged_Kick_Player", ip="203.0.113.79")
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
            requires_ack=False,
            acknowledged_at=datetime.now(),
            operation=operation,
        )

        data, err = await admin_management_service.apply_access_action(
            action="kick",
            target_type="player",
            target_value=player.nucleus_id,
            reason="NO_COVER",
            operator_name="unit-test",
        )

        self.assertIsNone(err)
        assert data is not None
        self.assertEqual(await PlayerAccessOperation.filter(player=player, action="ban").count(), 1)
        self.assertEqual(data["operation"]["action"], "ban")
        self.assertTrue(data["action_escalated"])
        self.assertEqual(data["previous_notice_id"], notice.id)
        self.assertFalse(data["sync_player_ip"])
        self.assertFalse(data["ip_synced"])
        self.assertEqual(await PlayerAccessRule.filter(rule_type="ip", source_action="ban", player=player).count(), 0)

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
