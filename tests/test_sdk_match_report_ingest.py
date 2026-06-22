import unittest
from datetime import datetime, timezone

from fastapi_service.scripts.rebuild_sdk_match_weapon_stats import rebuild_sdk_match_weapon_stats
from fastapi_service.services import match_service
from fastapi_service.tasks import refresh_player_kill_daily_stats
from shared_lib.models import Match, Player, PlayerKilled, PlayerMatchWeaponStat, SdkMatchEndReport
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


class SdkMatchReportIngestTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await Tortoise.init(config=TORTOISE_TEST_CONFIG)
        await Tortoise.generate_schemas()

    async def asyncTearDown(self) -> None:
        await Tortoise.close_connections()

    async def test_process_match_end_report_saves_weapon_stats_with_opponent_without_player_killed(self) -> None:
        ended_at = int(datetime.now(timezone.utc).timestamp())
        report = {
            "serverId": "sdk-test-server",
            "serverIp": "127.0.0.11",
            "serverPort": 37015,
            "map": "mp_rr_arena_phase_runner",
            "playlist": "fs_dm",
            "sdkVersion": "test",
            "tick": 12345,
            "spawnCount": 1,
            "endedAt": ended_at,
            "numPlayers": 2,
            "maxPlayers": 46,
            "players": [
                {
                    "uid": "1001",
                    "nucleusId": 1001,
                    "playerName": "attacker",
                    "inputDevice": "controller",
                    "weaponStats": [
                        {
                            "weapon": "mp_weapon_r97",
                            "shots": 10,
                            "hits": 5,
                            "bulletsHit": 5.0,
                            "damage": 123.5,
                            "headshots": 1,
                            "kills": 2,
                            "accuracy": 0.5,
                            "accuracyPercent": 50.0,
                        }
                    ],
                },
                {
                    "uid": "1002",
                    "nucleusId": 1002,
                    "playerName": "victim",
                    "inputDevice": "keyboard_mouse",
                },
            ],
            "killEvents": [
                {
                    "recordedAt": ended_at,
                    "attackerUid": "1001",
                    "attackerNucleusId": 1001,
                    "attackerName": "attacker",
                    "victimUid": "1002",
                    "victimNucleusId": 1002,
                    "victimName": "victim",
                    "weapon": "mp_weapon_r97",
                },
                {
                    "recordedAt": ended_at,
                    "attackerUid": "1001",
                    "attackerNucleusId": 1001,
                    "attackerName": "attacker",
                    "victimUid": "1002",
                    "victimNucleusId": 1002,
                    "victimName": "victim",
                    "weapon": "mp_weapon_r97",
                },
            ],
        }

        result = await match_service.process_match_end_report(report)

        self.assertEqual(result["kill_events"], 2)
        self.assertEqual(result["weapon_stats"], 2)
        self.assertEqual(await Match.all().count(), 1)
        self.assertEqual(await PlayerKilled.all().count(), 0)
        self.assertEqual(await PlayerMatchWeaponStat.all().count(), 2)

        attacker = await Player.get(nucleus_id=1001)
        victim = await Player.get(nucleus_id=1002)
        self.assertEqual(attacker.input_device, "controller")
        weapon_stat = await PlayerMatchWeaponStat.get(player=attacker, opponent_id__isnull=True)
        self.assertEqual(weapon_stat.shots, 10)
        self.assertEqual(weapon_stat.hits, 5)
        self.assertEqual(weapon_stat.kills, 2)
        self.assertEqual(weapon_stat.accuracy_percent, 50.0)
        self.assertEqual(weapon_stat.input_device, "controller")

        opponent_stat = await PlayerMatchWeaponStat.get(player=attacker, opponent=victim)
        self.assertEqual(opponent_stat.shots, 0)
        self.assertEqual(opponent_stat.hits, 0)
        self.assertEqual(opponent_stat.damage, 0)
        self.assertEqual(opponent_stat.kills, 2)
        self.assertEqual(opponent_stat.weapon, "mp_weapon_r97")
        self.assertEqual(opponent_stat.input_device, "controller")

        second_result = await match_service.process_match_end_report(report)

        self.assertEqual(second_result["kill_events"], 2)
        self.assertEqual(second_result["weapon_stats"], 2)
        self.assertEqual(await PlayerKilled.all().count(), 0)
        self.assertEqual(await PlayerMatchWeaponStat.all().count(), 2)

        match = await Match.first()
        report_row = await SdkMatchEndReport.first()
        self.assertIsNotNone(match)
        self.assertIsNotNone(report_row)
        assert match is not None
        assert report_row is not None
        await match.fetch_related("server")
        await PlayerMatchWeaponStat.create(
            player=attacker,
            opponent=None,
            match=match,
            server=match.server,
            weapon="stale_weapon",
            kills=99,
            input_device="controller",
            source="sdk_match_end",
        )
        self.assertEqual(await PlayerMatchWeaponStat.all().count(), 3)

        summary = await rebuild_sdk_match_weapon_stats(report_ids=[report_row.id], apply=True)

        self.assertEqual(summary.reports_rebuilt, 1)
        self.assertEqual(summary.old_weapon_rows, 3)
        self.assertEqual(summary.new_weapon_rows, 2)
        self.assertEqual(summary.kill_events_saved, 2)
        self.assertEqual(await PlayerMatchWeaponStat.all().count(), 2)
        self.assertEqual(await PlayerMatchWeaponStat.filter(weapon="stale_weapon").count(), 0)
        self.assertEqual(await PlayerMatchWeaponStat.filter(opponent=victim).count(), 1)

    async def test_process_match_end_report_does_not_infer_player_killed_from_weapon_stats(self) -> None:
        ended_at = int(datetime.now(timezone.utc).timestamp())
        report = {
            "serverIp": "127.0.0.12",
            "serverPort": 37015,
            "map": "mp_rr_arena_phase_runner",
            "playlist": "fs_dm",
            "sdkVersion": "test",
            "tick": 23456,
            "spawnCount": 1,
            "endedAt": ended_at,
            "numPlayers": 1,
            "maxPlayers": 46,
            "players": [
                {
                    "uid": "2001",
                    "nucleusId": 2001,
                    "playerName": "attacker",
                    "inputDevice": "controller",
                    "weaponStats": [
                        {
                            "weapon": "mp_weapon_r97",
                            "shots": 10,
                            "kills": 2,
                        }
                    ],
                },
            ],
            "killEvents": [],
        }

        result = await match_service.process_match_end_report(report)

        self.assertEqual(result["kill_events"], 0)
        self.assertEqual(result["weapon_stats"], 1)
        self.assertEqual(await PlayerKilled.all().count(), 0)
        self.assertEqual(await PlayerMatchWeaponStat.all().count(), 1)

    def test_daily_refresh_sql_includes_sdk_weapon_stats_without_opponent(self) -> None:
        self.assertIn("FROM player_match_weapon_stats pmws", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("NULL::int AS opponent_id", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("pmws.opponent_id", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("pmws.input_device", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("pk.attacker_id AS player_id", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("pk.victim_id AS opponent_id", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("pmws.opponent_id AS player_id", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("pmws.player_id AS opponent_id", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("pmws.player_id = pk.attacker_id", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("pmws.player_id = pk.victim_id", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("WHERE p.id = pmws.opponent_id", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("COALESCE(pk.category, '') <> 'sdk_match_end'", refresh_player_kill_daily_stats._INSERT_SQL)
        self.assertIn("NOT EXISTS", refresh_player_kill_daily_stats._INSERT_SQL)


if __name__ == "__main__":
    unittest.main()
