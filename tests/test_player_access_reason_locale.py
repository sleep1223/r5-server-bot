import unittest
from datetime import datetime, timedelta

from fastapi_service.core.cache import ACCESS_REPORT_TTL_SECONDS, server_cache
from fastapi_service.core.utils import CN_TZ, generate_hash
from fastapi_service.services import admin_management_service, admin_service, player_service, server_service
from fastapi_service.services import player_access_service as access_service
from fastapi_service.tasks import fetch_servers
from shared_lib.models import BanRecord, IpInfo, Player, PlayerAccessNotice, PlayerAccessOperation, PlayerAccessRule, Server
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
    CN_SERVER_KEY = "119.188.164.105:37015"
    HK_SERVER_KEY = "122.10.126.55:37015"

    GEO_BY_IP = {
        "1.2.3.4": ("中国", "山东"),
        "2.2.2.2": ("中国", "香港"),
        "8.8.8.8": ("美国", "加利福尼亚"),
        "8.8.4.4": ("美国", "加利福尼亚"),
        "203.0.113.8": ("日本", "东京"),
        "203.0.113.9": ("韩国", "首尔"),
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
        server_cache.servers.clear()
        server_cache.raw_response.clear()
        server_cache.ban_locations.clear()
        server_cache._access_reports.clear()

    async def asyncTearDown(self) -> None:
        access_service._resolve_geo = self._original_resolve_geo
        server_cache.servers.clear()
        server_cache.raw_response.clear()
        server_cache.ban_locations.clear()
        server_cache._access_reports.clear()
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
        server_id: str = CN_SERVER_KEY,
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
        assert global_rule is not None
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
        self.assertEqual(decision["reason"], "通信遅延が高すぎます。香港サーバーでプレイしてください")
        self.assertEqual(access_service.action_from_access_decision(decision), "kick")
        self.assertEqual(decision["rule_id"], access_service.GEO_POLICY_GLOBAL_RULE_ID)
        self.assertEqual(decision["rule"]["value"], access_service.GEO_POLICY_RULE_VALUE)
        self.assertEqual(decision["rule"]["matched_policy"], "domestic_server_foreign_player")

        global_rule = await PlayerAccessRule.get_or_none(rule_id=access_service.GEO_POLICY_GLOBAL_RULE_ID)
        self.assertIsNotNone(global_rule)
        assert global_rule is not None
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
        self.assertEqual(decision["reason"], "您的网络延迟过高，请选择国内服务器游玩")
        self.assertEqual(access_service.action_from_access_decision(decision), "kick")
        self.assertEqual(decision["rule"]["value"], access_service.GEO_POLICY_RULE_VALUE)
        self.assertEqual(decision["rule"]["matched_policy"], "overseas_server_domestic_player")

    async def test_region_lock_reasons_are_policy_text_not_kick_or_ban_text(self) -> None:
        reason = access_service.GEO_POLICY_FOREIGN_TO_DOMESTIC_REASON

        self.assertEqual(access_service.action_reason_text("kick", reason, locale="zh"), "您的网络延迟过高，请前往香港服务器游玩")
        self.assertEqual(access_service.action_reason_text("ban", reason, locale="zh"), "您的网络延迟过高，请前往香港服务器游玩")
        self.assertEqual(access_service.geo_policy_reason_text(reason, locale="en"), "Your latency is too high. Please play on a Hong Kong server")
        self.assertEqual(access_service.geo_policy_reason_text(reason, locale="ja"), "通信遅延が高すぎます。香港サーバーでプレイしてください")
        self.assertEqual(access_service.geo_policy_reason_text(reason, locale="ko"), "네트워크 지연 시간이 너무 높습니다. 홍콩 서버에서 플레이해 주세요")

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
            server_id=self.CN_SERVER_KEY,
            rule_id="server_geo_policy:cn-address:disabled",
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
            server_id=self.CN_SERVER_KEY,
            rule_id="server_geo_policy:cn-address:enabled",
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
        self.assertEqual(decision["rule_id"], "server_geo_policy:cn-address:enabled")
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
        decision = await self._check(uid="1000000000016", ip="::ffff:85cf:3e0")
        self.assertEqual(result["actions"][0]["reason"], decision["reason"])

    async def test_online_report_uses_same_localized_reason_for_pending_kick_notice(self) -> None:
        uid = "1000000000024"
        notice = await PlayerAccessNotice.create(uid=uid, action="kick", reason="NO_COVER", server_scope="global", requires_ack=True)

        result = await access_service.process_online_players_report(
            server_id="cn-server",
            report={
                "serverId": "cn-server",
                "serverIp": "::ffff:77bc:a469",
                "serverPort": 37015,
                "players": [
                    {
                        "uid": uid,
                        "nucleusId": int(uid),
                        "playerName": "pending-kick-player",
                        "ip": "1.2.3.4",
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
        self.assertEqual(result["actions"][0]["ruleId"], f"kick_notice:{notice.id}")
        decision = await self._check(uid=uid, ip="1.2.3.4")
        self.assertEqual(result["actions"][0]["reason"], decision["reason"])

    async def test_admin_kick_notice_reason_matches_online_and_reconnect_reason(self) -> None:
        uid = "1000000000025"
        player = await Player.create(
            nucleus_id=int(uid),
            nucleus_hash=generate_hash(uid),
            name="admin-kick-player",
            ip="8.8.8.8",
            country="美国",
            region="加利福尼亚",
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
        notice = await PlayerAccessNotice.get(uid=uid)
        self.assertEqual(notice.reason, "NO_COVER")
        self.assertIsNone(notice.message)

        result = await access_service.process_online_players_report(
            server_id="cn-server",
            report={
                "serverId": "cn-server",
                "serverIp": "::ffff:77bc:a469",
                "serverPort": 37015,
                "players": [
                    {
                        "uid": uid,
                        "nucleusId": int(uid),
                        "playerName": "admin-kick-player",
                        "ip": "8.8.8.8",
                        "port": 0,
                        "userId": 1,
                        "handle": 1,
                        "signonState": 6,
                    }
                ],
            },
        )
        decision = await self._check(uid=uid, ip="8.8.8.8")

        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["action"], "kick")
        self.assertEqual(result["actions"][0]["reason"], decision["reason"])
        self.assertEqual(result["actions"][0]["reason"], f"Kicked: No-cover rule violation. Visit {access_service.SELF_UNBAN_URL} to self-unban")

    async def test_reconnect_and_bans_use_operation_ip_locale_for_pending_kick(self) -> None:
        uid = "1000000000027"
        player = await Player.create(
            nucleus_id=int(uid),
            nucleus_hash=generate_hash(uid),
            name="operation-ip-kick",
            ip="8.8.8.8",
            country="美国",
            region="加利福尼亚",
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
        decision = await self._check(uid=uid, ip="1.2.3.4")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["source"], "kick_notice")
        self.assertEqual(decision["reason_locale"], "en")
        self.assertEqual(decision["reason"], f"Kicked: No-cover rule violation. Visit {access_service.SELF_UNBAN_URL} to self-unban")

        await player.refresh_from_db()
        self.assertEqual(player.country, "中国")
        rows, total = await admin_service.list_bans(page_size=20, offset=0, is_admin=True)

        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["operation_ip"], "8.8.8.8")
        self.assertEqual(rows[0]["operation_country"], "美国")
        self.assertEqual(rows[0]["operation_region"], "加利福尼亚")
        self.assertEqual(rows[0]["country"], "美国")
        self.assertEqual(rows[0]["region"], "加利福尼亚")
        self.assertEqual(rows[0]["player"]["country"], "美国")
        self.assertEqual(rows[0]["player"]["region"], "加利福尼亚")
        self.assertEqual(rows[0]["access"]["reason_locale"], "en")

    async def test_reconnect_and_bans_use_operation_ip_locale_for_uid_ban(self) -> None:
        uid = "1000000000028"
        player = await Player.create(
            nucleus_id=int(uid),
            nucleus_hash=generate_hash(uid),
            name="operation-ip-ban",
            ip="203.0.113.9",
            country="韩国",
            region="首尔",
        )

        data, err = await admin_management_service.apply_access_action(
            action="ban",
            target_type="player",
            target_value=player.nucleus_id,
            reason="CHEAT",
            operator_name="unit-test",
        )

        self.assertIsNone(err)
        assert data is not None
        decision = await self._check(uid=uid, ip="1.2.3.4")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["rule_type"], "uid")
        self.assertEqual(decision["reason_locale"], "ko")
        self.assertEqual(decision["reason"], "차단됨: 부정행위")

        await player.refresh_from_db()
        self.assertEqual(player.country, "中国")
        rows, total = await admin_service.list_bans(page_size=20, offset=0, is_admin=True)

        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["operation_ip"], "203.0.113.9")
        self.assertEqual(rows[0]["operation_country"], "韩国")
        self.assertEqual(rows[0]["operation_region"], "首尔")
        self.assertEqual(rows[0]["player"]["country"], "韩国")
        self.assertEqual(rows[0]["player"]["region"], "首尔")
        self.assertEqual(rows[0]["access"]["reason_locale"], "ko")

    async def test_online_report_populates_sdk_memory_cache_without_legacy_status(self) -> None:
        result = await access_service.process_online_players_report(
            server_id="cn-server",
            report={
                "serverId": "cn-server",
                "serverIp": "::ffff:77bc:a469",
                "serverPort": 37015,
                "map": "mp_rr_arena_phase_runner",
                "numPlayers": 1,
                "maxPlayers": 46,
                "players": [
                    {
                        "uid": "1000000000020",
                        "nucleusId": 1000000000020,
                        "playerName": "cache-player",
                        "ip": "::ffff:85cf:3e0",
                        "port": 0,
                        "userId": 1,
                        "handle": 1,
                        "signonState": 6,
                        "inputDevice": "keyboard_mouse",
                    }
                ],
            },
        )

        self.assertEqual(result["actions"], [])
        self.assertEqual(server_cache.servers, {})

        online_location = server_cache.get_online_location(1000000000020)
        self.assertIsNotNone(online_location)
        assert online_location is not None
        self.assertEqual(online_location["server_name"], "119.188.164.105:37015")
        self.assertEqual(online_location["server_host"], "119.188.164.105")
        self.assertEqual(online_location["server_port"], 37015)
        self.assertEqual(online_location["player_ip"], "133.207.3.224")
        self.assertTrue(online_location["_from_access_report"])

        online_servers = server_cache.get_online_servers()
        self.assertEqual(len(online_servers), 1)
        self.assertEqual(online_servers[0]["server_name"], "119.188.164.105:37015")
        self.assertEqual(online_servers[0]["server_host"], "119.188.164.105")

        players, total = await player_service.list_players(status="online")
        self.assertEqual(total, 1)
        self.assertEqual(players[0]["nucleus_id"], 1000000000020)
        self.assertEqual(players[0]["status"], "online")
        self.assertEqual(players[0]["ip"], "133.207.3.224")
        self.assertEqual(players[0]["country"], "日本")
        self.assertEqual(players[0]["region"], "东京")

        server_cache.update_raw_response({
            "servers": [
                {
                    "serverId": "raw-cn-server",
                    "name": "119.188.164.105:37015",
                    "region": "CN",
                    "map": "mp_rr_arena_phase_runner",
                    "numPlayers": 0,
                    "maxPlayers": 46,
                }
            ]
        })
        servers = await server_service.list_servers(cn_only=True, is_admin=True)
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "119.188.164.105:37015")
        self.assertEqual(servers[0]["player_count"], 1)
        self.assertEqual(servers[0]["players"][0]["nucleus_id"], 1000000000020)
        self.assertEqual(servers[0]["players"][0]["name"], "cache-player")
        self.assertEqual(servers[0]["players"][0]["country"], "日本")
        self.assertEqual(servers[0]["players"][0]["region"], "东京")

    async def test_query_players_resolves_address_only_online_cache_to_server_name(self) -> None:
        uid = 1000000000021
        await Server.create(
            host="119.188.164.105",
            port=37015,
            name="[CN(Jinan)] PWLA 1v1 Telecom",
            short_name="[CN(Jinan)]",
            has_status=True,
        )
        await Player.create(
            nucleus_id=uid,
            nucleus_hash=generate_hash(str(uid)),
            name="named-server-player",
            status="offline",
        )

        server_cache.update_access_report(
            "119.188.164.105:37015",
            {
                "serverIp": "119.188.164.105",
                "serverPort": 37015,
                "players": [
                    {
                        "uid": str(uid),
                        "nucleusId": uid,
                        "playerName": "named-server-player",
                        "inputDevice": "keyboard_mouse",
                    }
                ],
            },
        )

        rows = await player_service.query_players(str(uid), page_size=20, offset=0)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_online"])
        self.assertEqual(rows[0]["player"]["status"], "online")
        self.assertEqual(rows[0]["server"]["name"], "[CN(Jinan)] PWLA 1v1 Telecom")
        self.assertEqual(rows[0]["server"]["short_name"], "[CN(Jinan)]")
        self.assertEqual(rows[0]["server"]["host"], "119.188.164.105")
        self.assertEqual(rows[0]["server"]["port"], 37015)

    async def test_access_report_preserves_player_online_at_between_heartbeats(self) -> None:
        uid = 1000000000022
        server_key = "119.188.164.105:37015"
        payload = {
            "serverIp": "119.188.164.105",
            "serverPort": 37015,
            "serverName": "[CN(Jinan)] PWLA 1v1 Telecom",
            "players": [
                {
                    "uid": str(uid),
                    "nucleusId": uid,
                    "playerName": "duration-player",
                    "inputDevice": "keyboard_mouse",
                }
            ],
        }

        server_cache.update_access_report(server_key, payload)
        first_seen = datetime.now(CN_TZ) - timedelta(minutes=5)
        report = server_cache._access_reports[server_key]
        players = report["players"]
        assert isinstance(players, list)
        players[0]["online_at"] = first_seen

        server_cache.update_access_report(server_key, payload)

        online_location = server_cache.get_online_location(uid)
        self.assertIsNotNone(online_location)
        assert online_location is not None
        self.assertEqual(online_location["online_at"], first_seen)

        report = server_cache._access_reports[server_key]
        report["updated_at"] = datetime.now(CN_TZ) - timedelta(seconds=ACCESS_REPORT_TTL_SECONDS + 1)
        players = report["players"]
        assert isinstance(players, list)
        players[0]["online_at"] = first_seen

        server_cache.update_access_report(server_key, payload)

        reset_location = server_cache.get_online_location(uid)
        self.assertIsNotNone(reset_location)
        assert reset_location is not None
        self.assertNotEqual(reset_location["online_at"], first_seen)
        self.assertGreater(reset_location["online_at"], first_seen)

    async def test_sdk_reports_with_shared_config_server_id_are_scoped_by_address(self) -> None:
        shared_server_id = "shared-sdk-config-id"

        await access_service.process_online_players_report(
            server_id=shared_server_id,
            report={
                "serverId": shared_server_id,
                "serverIp": "1.2.3.4",
                "serverPort": 37015,
                "serverName": "[CN(A)] Alpha",
                "players": [],
            },
        )
        await access_service.process_online_players_report(
            server_id=shared_server_id,
            report={
                "serverId": shared_server_id,
                "serverIp": "2.2.2.2",
                "serverPort": 37016,
                "serverName": "[CN(B)] Beta",
                "players": [],
            },
        )

        rows = await Server.all().order_by("host", "port")
        self.assertEqual([(row.host, row.port) for row in rows], [("1.2.3.4", 37015), ("2.2.2.2", 37016)])
        self.assertTrue(all(row.server_id is None for row in rows))

        statuses = server_cache.get_online_server_statuses()
        self.assertEqual({status["_server"] for status in statuses}, {"1.2.3.4:37015", "2.2.2.2:37016"})

        server_cache.update_raw_response({
            "servers": [
                {
                    "serverId": "raw-alpha",
                    "name": "[CN(A)] Alpha",
                    "region": "CN",
                    "numPlayers": 0,
                    "maxPlayers": 20,
                },
                {
                    "serverId": "raw-beta",
                    "name": "[CN(B)] Beta",
                    "region": "CN",
                    "numPlayers": 0,
                    "maxPlayers": 20,
                },
            ]
        })
        listed = await server_service.list_servers(cn_only=True, is_admin=True)
        self.assertEqual({server["name"] for server in listed}, {"[CN(A)] Alpha", "[CN(B)] Beta"})
        self.assertEqual({server["short_name"] for server in listed}, {"[CN(A)]", "[CN(B)]"})

    async def test_sdk_report_matches_existing_server_by_unique_name_ignoring_case(self) -> None:
        await Server.create(
            server_id="raw-server-id",
            host="8.8.8.8",
            port=37015,
            name="[CN(Name)] Exact",
            has_status=True,
        )

        await access_service.process_online_players_report(
            server_id="shared-sdk-config-id",
            report={
                "serverId": "shared-sdk-config-id",
                "serverIp": "1.2.3.4",
                "serverPort": 37016,
                "serverName": "[cn(name)] exact",
                "players": [],
            },
        )

        rows = await Server.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].server_id, "raw-server-id")
        self.assertEqual(rows[0].host, "1.2.3.4")
        self.assertEqual(rows[0].port, 37016)

    async def test_sdk_report_updates_server_name_by_address(self) -> None:
        await Server.create(
            server_id="raw-server-id",
            host="1.2.3.4",
            port=37015,
            name="[CN(Old)] Old Name",
            has_status=True,
        )

        await access_service.process_online_players_report(
            server_id="ignored-sdk-config-id",
            report={
                "serverId": "ignored-sdk-config-id",
                "serverIp": "1.2.3.4",
                "serverPort": 37015,
                "serverName": "[CN(New)] New Name",
                "players": [],
            },
        )

        rows = await Server.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].server_id, "raw-server-id")
        self.assertEqual(rows[0].name, "[CN(New)] New Name")

    async def test_sdk_report_updates_server_port_by_unique_ip(self) -> None:
        await Server.create(
            server_id="raw-server-id",
            host="1.2.3.4",
            port=37015,
            name="[CN(Test)] Same Host",
            has_status=True,
        )

        await access_service.process_online_players_report(
            server_id="ignored-sdk-config-id",
            report={
                "serverId": "ignored-sdk-config-id",
                "serverIp": "1.2.3.4",
                "serverPort": 37016,
                "serverName": "[CN(Test)] Same Host",
                "players": [],
            },
        )

        rows = await Server.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].host, "1.2.3.4")
        self.assertEqual(rows[0].port, 37016)

    async def test_sdk_report_updates_server_address_by_unique_name(self) -> None:
        await Server.create(
            server_id="raw-server-id",
            host="1.2.3.4",
            port=37015,
            name="[CN(Test)] Stable Name",
            has_status=True,
        )

        await access_service.process_online_players_report(
            server_id="ignored-sdk-config-id",
            report={
                "serverId": "ignored-sdk-config-id",
                "serverIp": "2.2.2.2",
                "serverPort": 37016,
                "serverName": "[cn(test)] stable name",
                "players": [],
            },
        )

        rows = await Server.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].host, "2.2.2.2")
        self.assertEqual(rows[0].port, 37016)

    async def test_sdk_link_local_server_ip_matches_by_name_without_overwriting_host(self) -> None:
        await Server.create(
            server_id="raw-server-id",
            host="27.150.128.85",
            port=37015,
            name="[CN(Quanzhou2)] PWLA 1v1 Unicom 0.25AA",
            has_status=True,
        )

        await access_service.process_online_players_report(
            server_id="shared-sdk-config-id",
            report={
                "serverId": "shared-sdk-config-id",
                "serverIp": "fe80::f2ea:54c1:647e:98bd",
                "serverPort": 37015,
                "serverName": "[cn(quanzhou2)] pwla 1v1 unicom 0.25aa",
                "players": [],
            },
        )

        rows = await Server.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].host, "27.150.128.85")
        self.assertEqual(rows[0].port, 37015)

        statuses = server_cache.get_online_server_statuses()
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["_server"], "27.150.128.85:37015")
        self.assertEqual(statuses[0]["ip"], "27.150.128.85")

    async def test_server_list_matches_link_local_sdk_status_by_unique_name(self) -> None:
        await access_service.process_online_players_report(
            server_id="shared-sdk-config-id",
            report={
                "serverId": "shared-sdk-config-id",
                "serverIp": "fe80::f2ea:54c1:647e:98bd",
                "serverPort": 37015,
                "serverName": "[CN(Quanzhou2)] PWLA 1v1 Unicom 0.25AA",
                "players": [],
            },
        )
        server_cache.update_raw_response({
            "servers": [
                {
                    "serverId": "raw-quanzhou-id",
                    "ip": "27.150.128.85",
                    "port": 37015,
                    "name": "[CN(Quanzhou2)] PWLA 1v1 Unicom 0.25AA",
                    "region": "CN",
                    "playerCount": 6,
                    "maxPlayers": 34,
                }
            ]
        })

        rows = await Server.all()
        self.assertEqual(rows, [])

        listed = await server_service.list_servers(cn_only=True, is_admin=True)

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "[CN(Quanzhou2)] PWLA 1v1 Unicom 0.25AA")
        self.assertEqual(listed[0]["short_name"], "[CN(Quanzhou2)]")

    async def test_sdk_reports_without_usable_ip_are_scoped_by_server_name(self) -> None:
        shared_server_id = "shared-sdk-config-id"

        await access_service.process_online_players_report(
            server_id=shared_server_id,
            report={
                "serverId": shared_server_id,
                "serverIp": "",
                "serverPort": 37015,
                "serverName": "[CN(A)] Alpha",
                "players": [],
            },
        )
        await access_service.process_online_players_report(
            server_id=shared_server_id,
            report={
                "serverId": shared_server_id,
                "serverIp": "",
                "serverPort": 37015,
                "serverName": "[CN(B)] Beta",
                "players": [],
            },
        )

        statuses = server_cache.get_online_server_statuses()
        self.assertEqual({status["_server"] for status in statuses}, {"name:[cn(a)] alpha", "name:[cn(b)] beta"})
        self.assertEqual({status["hostname"] for status in statuses}, {"[CN(A)] Alpha", "[CN(B)] Beta"})

    async def test_server_list_filters_link_local_raw_server_rows(self) -> None:
        server_cache.update_raw_response({
            "servers": [
                {
                    "serverId": "bad-raw-id",
                    "ip": "fe80::e970:de6e:2c07:24c8",
                    "port": 37015,
                    "name": "[fe80::e970:de6e:2c07:24c8]:0:37015",
                    "region": "CN",
                    "playerCount": 0,
                    "maxPlayers": 34,
                },
                {
                    "serverId": "good-raw-id",
                    "ip": "106.75.246.225",
                    "port": 37015,
                    "name": "[CN(Shanghai)] PWLA 1v1 Telecom 0.3AA",
                    "region": "CN",
                    "playerCount": 1,
                    "maxPlayers": 26,
                },
            ]
        })

        listed = await server_service.list_servers(cn_only=True, is_admin=True)

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "[CN(Shanghai)] PWLA 1v1 Telecom 0.3AA")

    async def test_server_list_uses_raw_response_as_authoritative_source(self) -> None:
        await access_service.process_online_players_report(
            server_id="sdk-only-config-id",
            report={
                "serverId": "sdk-only-config-id",
                "serverIp": "1.2.3.4",
                "serverPort": 37015,
                "serverName": "[CN(Missing)] SDK Only",
                "players": [
                    {
                        "uid": "1000000000023",
                        "nucleusId": 1000000000023,
                        "playerName": "sdk-only-player",
                    }
                ],
            },
        )
        server_cache.update_raw_response({
            "servers": [
                {
                    "serverId": "raw-empty-id",
                    "name": "[CN(Empty)] Raw Empty",
                    "region": "CN",
                    "map": "mp_rr_arena_composite",
                    "playlist": "fs_1v1",
                    "numPlayers": 0,
                    "maxPlayers": 20,
                }
            ]
        })

        listed = await server_service.list_servers(cn_only=True, is_admin=True)

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "[CN(Empty)] Raw Empty")
        self.assertEqual(listed[0]["player_count"], 0)
        self.assertEqual(listed[0]["players"], [])
        self.assertNotIn("[CN(Missing)] SDK Only", {server["name"] for server in listed})

    async def test_server_list_matches_sdk_status_by_unique_name_ignoring_case(self) -> None:
        await access_service.process_online_players_report(
            server_id="shared-sdk-config-id",
            report={
                "serverId": "shared-sdk-config-id",
                "serverIp": "2.2.2.2",
                "serverPort": 37016,
                "serverName": "[cn(test)] real server",
                "players": [
                    {
                        "uid": "1000000000022",
                        "nucleusId": 1000000000022,
                        "playerName": "name-match-player",
                    }
                ],
            },
        )
        server_cache.update_raw_response({
            "servers": [
                {
                    "serverId": "raw-server-id",
                    "ip": "1.2.3.4",
                    "port": 37015,
                    "name": "[CN(Test)] Real Server",
                    "region": "CN",
                    "playerCount": 6,
                    "maxPlayers": 65,
                }
            ]
        })

        listed = await server_service.list_servers(cn_only=True, is_admin=True)

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "[CN(Test)] Real Server")
        self.assertEqual(listed[0]["short_name"], "[CN(Test)]")
        self.assertEqual(listed[0]["player_count"], 1)
        self.assertEqual(listed[0]["players"][0]["name"], "name-match-player")

    async def test_server_list_uses_address_ping_and_raw_short_name_before_sdk_config_id(self) -> None:
        await IpInfo.create(ip="1.2.3.4", country="中国", region="山东", ping=42, is_resolved=True)
        await access_service.process_online_players_report(
            server_id="shared-sdk-config-id",
            report={
                "serverId": "shared-sdk-config-id",
                "serverIp": "1.2.3.4",
                "serverPort": 37015,
                "players": [],
            },
        )
        server_cache.update_raw_response({
            "servers": [
                {
                    "serverId": "raw-server-id",
                    "ip": "1.2.3.4",
                    "port": 37015,
                    "name": "[CN(Test)] Real Server",
                    "region": "CN",
                    "playerCount": 3,
                    "maxPlayers": 65,
                }
            ]
        })

        listed = await server_service.list_servers(cn_only=True, is_admin=True)

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "[CN(Test)] Real Server")
        self.assertEqual(listed[0]["short_name"], "[CN(Test)]")
        self.assertEqual(listed[0]["ping"], 42)

    async def test_server_list_hides_ping_for_raw_only_servers(self) -> None:
        await IpInfo.create(ip="9.9.9.9", country="中国", region="山东", ping=12, is_resolved=True)
        server_cache.update_raw_response({
            "servers": [
                {
                    "serverId": "raw-only-id",
                    "ip": "9.9.9.9",
                    "port": 37015,
                    "name": "[CN(Raw)] Only",
                    "region": "CN",
                    "playerCount": 2,
                    "maxPlayers": 20,
                    "ping": 7,
                }
            ]
        })

        listed = await server_service.list_servers(cn_only=True, is_admin=True)

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "[CN(Raw)] Only")
        self.assertEqual(listed[0]["ping"], 0)

    async def test_server_list_sorts_by_players_then_player_count_then_ping(self) -> None:
        def players(prefix: str, count: int) -> list[dict]:
            return [
                {
                    "uid": f"10000000010{index}",
                    "nucleusId": 1000000001000 + index,
                    "playerName": f"{prefix}-{index}",
                }
                for index in range(count)
            ]

        await IpInfo.create(ip="1.1.1.1", country="中国", region="山东", ping=80, is_resolved=True)
        await IpInfo.create(ip="2.2.2.2", country="中国", region="山东", ping=20, is_resolved=True)
        await IpInfo.create(ip="3.3.3.3", country="中国", region="山东", ping=1, is_resolved=True)
        await IpInfo.create(ip="6.6.6.6", country="中国", region="山东", ping=5, is_resolved=True)
        server_cache.update_access_report(
            "1.1.1.1:37015",
            {
                "serverIp": "1.1.1.1",
                "serverPort": 37015,
                "serverName": "[CN(Sort)] High",
                "players": players("high", 5),
                "maxPlayers": 20,
            },
        )
        server_cache.update_access_report(
            "2.2.2.2:37015",
            {
                "serverIp": "2.2.2.2",
                "serverPort": 37015,
                "serverName": "[CN(Sort)] Low",
                "players": players("low", 5),
                "maxPlayers": 20,
            },
        )
        server_cache.update_access_report(
            "6.6.6.6:37015",
            {
                "serverIp": "6.6.6.6",
                "serverPort": 37015,
                "serverName": "[CN(Sort)] Reported Empty",
                "players": [],
                "maxPlayers": 20,
            },
        )
        server_cache.update_raw_response({
            "servers": [
                {
                    "serverId": "empty-id",
                    "ip": "4.4.4.4",
                    "port": 37015,
                    "name": "[CN(Sort)] Empty",
                    "region": "CN",
                    "playerCount": 0,
                    "maxPlayers": 20,
                },
                {
                    "serverId": "raw-equal-id",
                    "ip": "3.3.3.3",
                    "port": 37015,
                    "name": "[CN(Sort)] Raw Equal",
                    "region": "CN",
                    "playerCount": 5,
                    "maxPlayers": 20,
                    "ping": 1,
                },
                {
                    "serverId": "high-id",
                    "ip": "1.1.1.1",
                    "port": 37015,
                    "name": "[CN(Sort)] High",
                    "region": "CN",
                    "playerCount": 0,
                    "maxPlayers": 20,
                },
                {
                    "serverId": "three-id",
                    "ip": "5.5.5.5",
                    "port": 37015,
                    "name": "[CN(Sort)] Three",
                    "region": "CN",
                    "playerCount": 3,
                    "maxPlayers": 20,
                },
                {
                    "serverId": "reported-empty-id",
                    "ip": "6.6.6.6",
                    "port": 37015,
                    "name": "[CN(Sort)] Reported Empty",
                    "region": "CN",
                    "playerCount": 0,
                    "maxPlayers": 20,
                },
                {
                    "serverId": "low-id",
                    "ip": "2.2.2.2",
                    "port": 37015,
                    "name": "[CN(Sort)] Low",
                    "region": "CN",
                    "playerCount": 0,
                    "maxPlayers": 20,
                },
            ]
        })

        listed = await server_service.list_servers(cn_only=True, is_admin=True)

        self.assertEqual(
            [server["name"] for server in listed],
            [
                "[CN(Sort)] Low",
                "[CN(Sort)] High",
                "[CN(Sort)] Raw Equal",
                "[CN(Sort)] Three",
                "[CN(Sort)] Reported Empty",
                "[CN(Sort)] Empty",
            ],
        )
        self.assertEqual({server["name"]: server["ping"] for server in listed}["[CN(Sort)] Raw Equal"], 0)

    async def test_fetch_server_task_refreshes_reported_server_pings(self) -> None:
        server_cache.update_access_report(
            "1.2.3.4:37015",
            {
                "serverIp": "1.2.3.4",
                "serverPort": 37015,
                "serverName": "[CN(Ping)] Reported",
                "players": [],
            },
        )
        original_ping = fetch_servers.get_local_ping

        async def fake_ping(host: str) -> int:
            self.assertEqual(host, "1.2.3.4")
            return 33

        fetch_servers.get_local_ping = fake_ping
        try:
            refreshed = await fetch_servers._refresh_reported_server_pings()
        finally:
            fetch_servers.get_local_ping = original_ping

        info = await IpInfo.get(ip="1.2.3.4")
        self.assertEqual(refreshed, 1)
        self.assertEqual(info.ping, 33)

    async def test_online_report_accepts_player_without_ip(self) -> None:
        result = await access_service.process_online_players_report(
            server_id="cn-server",
            report={
                "serverId": "cn-server",
                "serverIp": "::ffff:77bc:a469",
                "serverPort": 37015,
                "map": "mp_rr_arena_phase_runner",
                "numPlayers": 1,
                "maxPlayers": 46,
                "players": [
                    {
                        "uid": "1000000000021",
                        "nucleusId": 1000000000021,
                        "playerName": "no-ip-player",
                        "userId": 1,
                        "handle": 1,
                        "signonState": 6,
                    }
                ],
            },
        )

        self.assertEqual(result["actions"], [])

        online_location = server_cache.get_online_location(1000000000021)
        self.assertIsNotNone(online_location)
        assert online_location is not None
        self.assertIsNone(online_location["player_ip"])
        self.assertIsNone(online_location["player_country"])
        self.assertIsNone(online_location["player_region"])

        server_cache.update_raw_response({
            "servers": [
                {
                    "serverId": "raw-cn-server",
                    "name": "119.188.164.105:37015",
                    "region": "CN",
                    "map": "mp_rr_arena_phase_runner",
                    "numPlayers": 0,
                    "maxPlayers": 46,
                }
            ]
        })
        servers = await server_service.list_servers(cn_only=True, is_admin=True)
        self.assertEqual(servers[0]["players"][0]["name"], "no-ip-player")
        self.assertIsNone(servers[0]["players"][0]["country"])
        self.assertIsNone(servers[0]["players"][0]["region"])

    async def test_domestic_ip_blocked_from_hong_kong_server_returns_chinese_reason(self) -> None:
        await self._deny_rule(
            rule_type="country",
            value="中国",
            reason="NO_COVER",
            source_action="ban",
            server_id=self.HK_SERVER_KEY,
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

    async def test_cidr_rule_returns_korean_reason_for_korean_ip(self) -> None:
        await self._deny_rule(rule_type="cidr", value="203.0.113.0/24", reason="CHEAT", source_action="ban", rule_id="deny-cidr-kr")

        decision = await self._check(uid="1000000000026", ip="203.0.113.9")

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason_locale"], "ko")
        self.assertEqual(decision["reason"], "차단됨: 부정행위")
        self.assertEqual(decision["rule"]["rule_type"], "cidr")

    async def test_region_rule_returns_chinese_reason_for_hong_kong_ip(self) -> None:
        await self._deny_rule(
            rule_type="region",
            value="香港",
            reason="RULES",
            source_action="kick",
            server_id=self.HK_SERVER_KEY,
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

    async def test_create_access_notice_overwrites_pending_kick_notice(self) -> None:
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
        self.assertEqual(second_notice.message, "second")
        self.assertEqual(second_notice.message_context["remark"], "second")
        self.assertTrue(second_notice.message_context["pending_notice_reused"])


if __name__ == "__main__":
    unittest.main()
