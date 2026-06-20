import unittest

from fastapi_service.core.utils import generate_hash
from fastapi_service.services import player_access_service as access_service
from shared_lib.models import BanRecord, Player, PlayerAccessNotice, PlayerAccessOperation, PlayerAccessRule
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


class PlayerAccessReasonLocaleTest(unittest.IsolatedAsyncioTestCase):
    GEO_BY_IP = {
        "1.2.3.4": ("中国", "山东"),
        "2.2.2.2": ("中国", "香港"),
        "8.8.8.8": ("美国", "加利福尼亚"),
        "8.8.4.4": ("美国", "加利福尼亚"),
        "203.0.113.8": ("日本", "东京"),
        "119.188.164.105": ("中国", "山东"),
        "133.207.3.224": ("日本", "东京"),
        "5.6.7.8": ("德国", "柏林"),
        "122.10.126.55": ("中国", "香港"),
    }

    async def asyncSetUp(self) -> None:
        await Tortoise.init(config=TORTOISE_TEST_CONFIG)
        await Tortoise.generate_schemas()
        self._original_resolve_geo = access_service._resolve_geo
        access_service._resolve_geo = self._fake_resolve_geo

    async def asyncTearDown(self) -> None:
        access_service._resolve_geo = self._original_resolve_geo
        await Tortoise.close_connections()

    async def _fake_resolve_geo(self, ip: str) -> tuple[str | None, str | None]:
        return self.GEO_BY_IP.get(ip, (None, None))

    async def _deny_rule(
        self,
        *,
        rule_type: str,
        value: str,
        reason: str,
        source_action: str = "ban",
        server_id: str = "cn-server",
        rule_id: str | None = None,
    ) -> PlayerAccessRule:
        return await PlayerAccessRule.create(
            rule_type=rule_type,
            action="deny",
            value=value,
            server_scope="server",
            server_id=server_id,
            reason=reason,
            rule_id=rule_id or f"{server_id}:{rule_type}:{value}",
            source_action=source_action,
            priority=10,
        )

    async def _global_geo_policy_rule(self, *, enabled: bool = True) -> PlayerAccessRule:
        return await PlayerAccessRule.create(
            rule_type=access_service.GEO_POLICY_RULE_TYPE,
            action="deny",
            value=access_service.GEO_POLICY_RULE_VALUE,
            server_scope="global",
            rule_id=access_service.GEO_POLICY_GLOBAL_RULE_ID,
            reason=access_service.REGION_LOCK_REASON,
            source_action="kick",
            enabled=enabled,
        )

    async def _check(
        self,
        *,
        uid: str = "1000000000001",
        ip: str,
        server_id: str = "cn-server",
        server_ip: str = "119.188.164.105",
    ) -> dict:
        return await access_service.check_player_access(
            uid=uid,
            nucleus_id=int(uid),
            player_name=f"player-{uid}",
            ip=ip,
            port=0,
            server_id=server_id,
            server_ip=server_ip,
            server_port=37015,
        )

    async def _banned_player(self, *, uid: str, reason: str) -> Player:
        player = await Player.create(
            nucleus_id=int(uid),
            nucleus_hash=generate_hash(uid),
            name=f"banned-{uid}",
            status="banned",
        )
        await BanRecord.create(player=player, reason=reason, operator="unit-test")
        return player

    async def test_overseas_ip_blocked_from_domestic_server_returns_english_reason(self) -> None:
        await self._deny_rule(rule_type="country", value="美国", reason="RULES", source_action="kick", rule_id="deny-us-cn")

        decision = await self._check(uid="1000000000002", ip="8.8.8.8")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason_locale"], "en")
        self.assertEqual(decision["reason"], "Kicked: Rule violation")
        self.assertEqual(access_service.action_from_access_decision(decision), "kick")
        self.assertEqual(decision["rule"]["rule_id"], "deny-us-cn")

    async def test_default_geo_policy_is_created_disabled_and_allows_ipv4_mapped_japanese_ip(self) -> None:
        decision = await self._check(
            uid="1000000000012",
            ip="::ffff:85cf:3e0",
            server_id="cn-server",
            server_ip="::ffff:77bc:a469",
        )

        self.assertEqual(access_service._normalize_ip("::ffff:85cf:3e0"), "133.207.3.224")
        self.assertEqual(access_service._normalize_ip("::ffff:77bc:a469"), "119.188.164.105")
        self.assertTrue(decision["allow"])
        self.assertEqual(decision["source"], "default_allow")

        global_rule = await PlayerAccessRule.get_or_none(rule_id=access_service.GEO_POLICY_GLOBAL_RULE_ID)
        self.assertIsNotNone(global_rule)
        self.assertFalse(global_rule.enabled)
        self.assertEqual(global_rule.server_scope, "global")

    async def test_enabled_global_geo_policy_blocks_ipv4_mapped_japanese_ip_from_mainland_server(self) -> None:
        await self._global_geo_policy_rule()

        decision = await self._check(
            uid="1000000000012",
            ip="::ffff:85cf:3e0",
            server_id="cn-server",
            server_ip="::ffff:77bc:a469",
        )

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["source"], "server_geo_policy")
        self.assertEqual(decision["reason_locale"], "ja")
        self.assertEqual(decision["reason"], "キック: 通信遅延が高すぎます。香港サーバーでプレイしてください")
        self.assertEqual(access_service.action_from_access_decision(decision), "kick")
        self.assertEqual(decision["rule_id"], access_service.GEO_POLICY_GLOBAL_RULE_ID)
        self.assertEqual(decision["rule"]["value"], access_service.GEO_POLICY_RULE_VALUE)
        self.assertEqual(decision["rule"]["matched_policy"], "domestic_server_foreign_player")

        global_rule = await PlayerAccessRule.get_or_none(rule_id=access_service.GEO_POLICY_GLOBAL_RULE_ID)
        self.assertIsNotNone(global_rule)
        self.assertTrue(global_rule.enabled)
        self.assertEqual(global_rule.server_scope, "global")

    async def test_ip_rule_payload_normalizes_ipv4_mapped_ipv6(self) -> None:
        payload = access_service.normalize_access_rule_payload(
            rule_type="ip",
            action="deny",
            value="::ffff:85cf:3e0",
            server_scope="global",
        )

        self.assertEqual(payload["value"], "133.207.3.224")

    async def test_geo_policy_rule_payload_normalizes_to_canonical_value(self) -> None:
        payload = access_service.normalize_access_rule_payload(
            rule_type="geo_policy",
            action="deny",
            value="server_geo_policy",
            server_scope="global",
        )

        self.assertEqual(payload["value"], access_service.GEO_POLICY_RULE_VALUE)
        with self.assertRaises(ValueError):
            access_service.normalize_access_rule_payload(
                rule_type="geo_policy",
                action="allow",
                value=access_service.GEO_POLICY_RULE_VALUE,
                server_scope="global",
            )

    async def test_mainland_ip_is_blocked_from_hong_kong_server_by_enabled_global_policy(self) -> None:
        await self._global_geo_policy_rule()

        decision = await self._check(uid="1000000000013", ip="1.2.3.4", server_id="hk-server", server_ip="122.10.126.55")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["source"], "server_geo_policy")
        self.assertEqual(decision["reason_locale"], "zh")
        self.assertEqual(decision["reason"], "已被踢出: 您的网络延迟过高，请选择国内服务器游玩")
        self.assertEqual(access_service.action_from_access_decision(decision), "kick")
        self.assertEqual(decision["rule"]["value"], access_service.GEO_POLICY_RULE_VALUE)
        self.assertEqual(decision["rule"]["matched_policy"], "overseas_server_domestic_player")

    async def test_global_geo_policy_can_be_disabled(self) -> None:
        await PlayerAccessRule.create(
            rule_type=access_service.GEO_POLICY_RULE_TYPE,
            action="deny",
            value=access_service.GEO_POLICY_RULE_VALUE,
            server_scope="global",
            rule_id=access_service.GEO_POLICY_GLOBAL_RULE_ID,
            reason=access_service.REGION_LOCK_REASON,
            source_action="kick",
            enabled=False,
        )

        decision = await self._check(
            uid="1000000000017",
            ip="::ffff:85cf:3e0",
            server_id="cn-server",
            server_ip="::ffff:77bc:a469",
        )

        self.assertTrue(decision["allow"])
        self.assertEqual(decision["source"], "default_allow")

    async def test_server_disabled_geo_policy_overrides_enabled_global_policy(self) -> None:
        await PlayerAccessRule.create(
            rule_type=access_service.GEO_POLICY_RULE_TYPE,
            action="deny",
            value=access_service.GEO_POLICY_RULE_VALUE,
            server_scope="global",
            rule_id=access_service.GEO_POLICY_GLOBAL_RULE_ID,
            reason=access_service.REGION_LOCK_REASON,
            source_action="kick",
            enabled=True,
        )
        await PlayerAccessRule.create(
            rule_type=access_service.GEO_POLICY_RULE_TYPE,
            action="deny",
            value=access_service.GEO_POLICY_RULE_VALUE,
            server_scope="server",
            server_id="cn-server",
            rule_id="server_geo_policy:cn-server:disabled",
            reason=access_service.REGION_LOCK_REASON,
            source_action="kick",
            enabled=False,
        )

        decision = await self._check(
            uid="1000000000018",
            ip="::ffff:85cf:3e0",
            server_id="cn-server",
            server_ip="::ffff:77bc:a469",
        )

        self.assertTrue(decision["allow"])
        self.assertEqual(decision["source"], "default_allow")

    async def test_server_enabled_geo_policy_overrides_disabled_global_policy(self) -> None:
        await PlayerAccessRule.create(
            rule_type=access_service.GEO_POLICY_RULE_TYPE,
            action="deny",
            value=access_service.GEO_POLICY_RULE_VALUE,
            server_scope="global",
            rule_id=access_service.GEO_POLICY_GLOBAL_RULE_ID,
            reason=access_service.REGION_LOCK_REASON,
            source_action="kick",
            enabled=False,
        )
        await PlayerAccessRule.create(
            rule_type=access_service.GEO_POLICY_RULE_TYPE,
            action="deny",
            value=access_service.GEO_POLICY_RULE_VALUE,
            server_scope="server",
            server_id="cn-server",
            rule_id="server_geo_policy:cn-server:enabled",
            reason=access_service.REGION_LOCK_REASON,
            source_action="kick",
            enabled=True,
        )

        decision = await self._check(
            uid="1000000000019",
            ip="::ffff:85cf:3e0",
            server_id="cn-server",
            server_ip="::ffff:77bc:a469",
        )

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["rule_id"], "server_geo_policy:cn-server:enabled")
        self.assertEqual(decision["rule"]["matched_policy"], "domestic_server_foreign_player")

    async def test_mainland_ip_is_allowed_on_mainland_server_by_default_policy(self) -> None:
        decision = await self._check(uid="1000000000014", ip="1.2.3.4")

        self.assertTrue(decision["allow"])
        self.assertEqual(decision["source"], "default_allow")

    async def test_overseas_ip_is_allowed_on_overseas_server_by_default_policy(self) -> None:
        decision = await self._check(uid="1000000000015", ip="8.8.8.8", server_id="us-server", server_ip="8.8.4.4")

        self.assertTrue(decision["allow"])
        self.assertEqual(decision["source"], "default_allow")

    async def test_online_report_returns_geo_policy_kick_action(self) -> None:
        await self._global_geo_policy_rule()

        result = await access_service.process_online_players_report(
            server_id="cn-server",
            report={
                "serverId": "cn-server",
                "serverIp": "::ffff:77bc:a469",
                "serverPort": 37015,
                "players": [
                    {
                        "uid": "1000000000016",
                        "nucleusId": 1000000000016,
                        "playerName": "jp-player",
                        "ip": "::ffff:85cf:3e0",
                        "port": 0,
                        "userId": 1,
                        "handle": 1,
                        "signonState": 6,
                    }
                ],
            },
        )

        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["action"], "kick")
        self.assertEqual(result["actions"][0]["ruleId"], access_service.GEO_POLICY_GLOBAL_RULE_ID)
        self.assertEqual(result["actions"][0]["reason"], "キック: 通信遅延が高すぎます。香港サーバーでプレイしてください")

    async def test_domestic_ip_blocked_from_hong_kong_server_returns_chinese_reason(self) -> None:
        await self._deny_rule(
            rule_type="country",
            value="中国",
            reason="NO_COVER",
            source_action="ban",
            server_id="hk-server",
            rule_id="deny-cn-hk",
        )

        decision = await self._check(uid="1000000000003", ip="1.2.3.4", server_id="hk-server", server_ip="122.10.126.55")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason_locale"], "zh")
        self.assertEqual(decision["reason"], "已被封禁: 撤回掩体")
        self.assertEqual(access_service.action_from_access_decision(decision), "ban")
        self.assertEqual(decision["rule"]["rule_id"], "deny-cn-hk")

    async def test_japanese_banned_player_returns_japanese_reason(self) -> None:
        await self._banned_player(uid="1000000000004", reason="CHEAT")

        decision = await self._check(uid="1000000000004", ip="203.0.113.8")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["source"], "legacy_ban")
        self.assertEqual(decision["reason_locale"], "ja")
        self.assertEqual(decision["reason"], "参加禁止: チート行為")

    async def test_europe_banned_player_returns_english_reason(self) -> None:
        await self._banned_player(uid="1000000000005", reason="BE_POLITE")

        decision = await self._check(uid="1000000000005", ip="5.6.7.8")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["source"], "legacy_ban")
        self.assertEqual(decision["reason_locale"], "en")
        self.assertEqual(decision["reason"], "Banned: Inappropriate behavior")

    async def test_chinese_banned_player_returns_chinese_reason(self) -> None:
        await self._banned_player(uid="1000000000006", reason="CHEAT")

        decision = await self._check(uid="1000000000006", ip="1.2.3.4")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["source"], "legacy_ban")
        self.assertEqual(decision["reason_locale"], "zh")
        self.assertEqual(decision["reason"], "已被封禁: 作弊")

    async def test_exact_ip_rule_keeps_custom_reason_text(self) -> None:
        await self._deny_rule(rule_type="ip", value="8.8.4.4", reason="Custom maintenance block", source_action="kick", rule_id="deny-ip-custom")

        decision = await self._check(uid="1000000000007", ip="8.8.4.4")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason_locale"], "en")
        self.assertEqual(decision["reason"], "Custom maintenance block")
        self.assertEqual(decision["rule"]["rule_type"], "ip")

    async def test_cidr_rule_returns_english_reason_for_us_ip(self) -> None:
        await self._deny_rule(rule_type="cidr", value="8.8.8.0/24", reason="CHEAT", source_action="ban", rule_id="deny-cidr-us")

        decision = await self._check(uid="1000000000008", ip="8.8.8.8")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason_locale"], "en")
        self.assertEqual(decision["reason"], "Banned: Cheating")
        self.assertEqual(decision["rule"]["rule_type"], "cidr")

    async def test_region_rule_returns_chinese_reason_for_hong_kong_ip(self) -> None:
        await self._deny_rule(
            rule_type="region",
            value="香港",
            reason="RULES",
            source_action="kick",
            server_id="hk-server",
            rule_id="deny-hk-region",
        )

        decision = await self._check(uid="1000000000009", ip="122.10.126.55", server_id="hk-server", server_ip="122.10.126.55")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason_locale"], "zh")
        self.assertEqual(decision["reason"], "已被踢出: 违反规则")
        self.assertEqual(decision["rule"]["rule_type"], "region")

    async def test_pending_notice_has_priority_and_uses_source_ip_locale(self) -> None:
        uid = "1000000000010"
        await PlayerAccessNotice.create(uid=uid, action="kick", reason="RULES", server_scope="global", requires_ack=True)
        await self._deny_rule(rule_type="country", value="美国", reason="CHEAT", source_action="ban", rule_id="lower-priority-country")

        decision = await self._check(uid=uid, ip="8.8.8.8")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["source"], "kick_notice")
        self.assertEqual(decision["reason_locale"], "en")
        self.assertEqual(decision["reason"], "Kicked: Rule violation. Visit r5.sleep0.de/bans to self-unban")

    async def test_uid_allow_rule_overrides_legacy_ban(self) -> None:
        uid = "1000000000011"
        await self._banned_player(uid=uid, reason="CHEAT")
        await PlayerAccessRule.create(
            rule_type="uid",
            action="allow",
            value=uid,
            server_scope="global",
            reason="RULES",
            rule_id="allow-banned-uid",
            priority=1,
        )

        decision = await self._check(uid=uid, ip="8.8.8.8")

        self.assertTrue(decision["allow"])
        self.assertIsNone(decision["reason"])
        self.assertEqual(decision["rule"]["rule_id"], "allow-banned-uid")

    async def test_legacy_access_sync_backfills_bans_and_kick_notices_once(self) -> None:
        banned = await Player.create(
            nucleus_id=1000000000101,
            nucleus_hash=generate_hash("1000000000101"),
            name="legacy-banned",
            status="banned",
        )
        await BanRecord.create(player=banned, reason="NO_COVER", operator="legacy-admin")

        kicked = await Player.create(
            nucleus_id=1000000000102,
            nucleus_hash=generate_hash("1000000000102"),
            name="legacy-kicked",
            status="offline",
            kick_count=1,
        )

        banned_with_kick_count = await Player.create(
            nucleus_id=1000000000103,
            nucleus_hash=generate_hash("1000000000103"),
            name="legacy-banned-kicked",
            status="banned",
            kick_count=1,
        )
        await BanRecord.create(player=banned_with_kick_count, reason="CHEAT", operator="legacy-admin")

        existing_notice_player = await Player.create(
            nucleus_id=1000000000104,
            nucleus_hash=generate_hash("1000000000104"),
            name="legacy-kicked-existing",
            status="offline",
            kick_count=1,
        )
        await PlayerAccessNotice.create(
            player=existing_notice_player,
            uid=str(existing_notice_player.nucleus_id),
            action="kick",
            reason="RULES",
            requires_ack=True,
        )

        stats = await access_service.sync_legacy_access_records()

        self.assertEqual(stats["ban_rules_created"], 2)
        self.assertEqual(stats["kick_notices_created"], 1)
        self.assertTrue(await PlayerAccessRule.filter(rule_type="uid", value=str(banned.nucleus_id), enabled=True).exists())
        self.assertTrue(await PlayerAccessRule.filter(rule_type="uid", value=str(banned_with_kick_count.nucleus_id), enabled=True).exists())
        self.assertEqual(await PlayerAccessNotice.filter(uid=str(kicked.nucleus_id)).count(), 1)
        self.assertEqual(await PlayerAccessNotice.filter(uid=str(banned_with_kick_count.nucleus_id)).count(), 0)
        self.assertEqual(await PlayerAccessNotice.filter(uid=str(existing_notice_player.nucleus_id)).count(), 1)

        second_stats = await access_service.sync_legacy_access_records()

        self.assertEqual(second_stats["ban_rules_created"], 0)
        self.assertEqual(second_stats["kick_notices_created"], 0)

    async def test_create_access_notice_reuses_pending_kick_notice(self) -> None:
        player = await Player.create(
            nucleus_id=1000000000105,
            nucleus_hash=generate_hash("1000000000105"),
            name="pending-kick-reuse",
            status="kicked",
        )
        first_operation = await PlayerAccessOperation.create(
            action="kick",
            target_type="player",
            target_value=str(player.nucleus_id),
            normalized_target=str(player.nucleus_id),
            server_scope="global",
            reason="RULES",
            player=player,
        )
        second_operation = await PlayerAccessOperation.create(
            action="kick",
            target_type="player",
            target_value=str(player.nucleus_id),
            normalized_target=str(player.nucleus_id),
            server_scope="global",
            reason="NO_COVER",
            player=player,
        )

        first_notice = await access_service.create_access_notice(
            player=player,
            uid=player.nucleus_id,
            action="kick",
            reason="RULES",
            message="first",
            message_context={"remark": "first"},
            server_scope="global",
            server_id=None,
            operation=first_operation,
        )
        second_notice = await access_service.create_access_notice(
            player=player,
            uid=player.nucleus_id,
            action="kick",
            reason="NO_COVER",
            message="second",
            message_context={"remark": "second"},
            server_scope="global",
            server_id=None,
            operation=second_operation,
        )

        self.assertEqual(first_notice.id, second_notice.id)
        self.assertEqual(await PlayerAccessNotice.filter(uid=str(player.nucleus_id), requires_ack=True).count(), 1)
        self.assertEqual(second_notice.reason, "NO_COVER")
        self.assertEqual(getattr(second_notice, "operation_id", None), second_operation.id)
        self.assertEqual(second_notice.message_context["remark"], "second")
        self.assertTrue(second_notice.message_context["pending_notice_reused"])


if __name__ == "__main__":
    unittest.main()
