import unittest
from datetime import datetime, timezone

from fastapi_service.services import leaderboard_service
from fastapi_service.tasks import refresh_player_kill_daily_stats


class LeaderboardKdInputDeviceTest(unittest.IsolatedAsyncioTestCase):
    async def test_kd_ranking_passes_input_device_filter_to_daily_stats(self) -> None:
        calls: dict = {}
        originals = (
            leaderboard_service.get_date_range,
            leaderboard_service._get_excluded_server_ids,
            leaderboard_service._aggregate_kills_deaths,
            leaderboard_service._enrich_with_player_names,
        )

        def fake_get_date_range(_range_type: str):
            return (
                datetime(2026, 6, 26, tzinfo=timezone.utc),
                datetime(2026, 6, 26, 23, 59, 59, tzinfo=timezone.utc),
            )

        async def fake_get_excluded_server_ids():
            return []

        async def fake_aggregate_kills_deaths(*args, **kwargs):
            calls.update(kwargs)
            return {42: {"kills": 10, "deaths": 5}}

        async def fake_enrich_with_player_names(*args, **kwargs):
            return {42: {"id": 42, "name": "sample", "nucleus_id": 10042, "status": "normal", "input_device": "unknown"}}

        leaderboard_service.get_date_range = fake_get_date_range
        leaderboard_service._get_excluded_server_ids = fake_get_excluded_server_ids
        leaderboard_service._aggregate_kills_deaths = fake_aggregate_kills_deaths
        leaderboard_service._enrich_with_player_names = fake_enrich_with_player_names
        try:
            results, total = await leaderboard_service.get_kd_ranking(
                range_type="today",
                sort="kd",
                min_kills=1,
                min_deaths=0,
                offset=0,
                page_size=100,
                input_device="controller",
            )
        finally:
            (
                leaderboard_service.get_date_range,
                leaderboard_service._get_excluded_server_ids,
                leaderboard_service._aggregate_kills_deaths,
                leaderboard_service._enrich_with_player_names,
            ) = originals

        self.assertEqual(total, 1)
        self.assertEqual(results[0]["input_device"], "controller")
        self.assertEqual(results[0]["deaths"], 5)
        self.assertEqual(calls["extra_filters"], {"input_device": "controller"})

    def test_daily_refresh_death_rows_use_victim_match_input_device(self) -> None:
        sql = refresh_player_kill_daily_stats._INSERT_SQL

        self.assertIn("FROM player_match_weapon_stats pmws_victim", sql)
        self.assertIn("pmws_victim.match_id = pmws.match_id", sql)
        self.assertIn("pmws_victim.player_id = pmws.opponent_id", sql)


if __name__ == "__main__":
    unittest.main()
