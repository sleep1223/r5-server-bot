import unittest
from unittest.mock import AsyncMock, patch

from fastapi_service.api.v1 import access


class AccessReasonPeriodTest(unittest.IsolatedAsyncioTestCase):
    def test_client_disconnect_reason_enforces_ascii_period(self) -> None:
        self.assertIsNone(access._client_disconnect_reason(None))
        self.assertIsNone(access._client_disconnect_reason("  "))
        self.assertEqual(access._client_disconnect_reason("测试文案"), "测试文案.")
        self.assertEqual(access._client_disconnect_reason("测试文案。"), "测试文案。.")
        self.assertEqual(access._client_disconnect_reason("Already safe."), "Already safe.")

    @patch.object(access.player_access_service, "check_player_access", new_callable=AsyncMock)
    async def test_check_response_enforces_period_on_both_reasons(self, check_mock: AsyncMock) -> None:
        check_mock.return_value = {
            "allow": False,
            "reason": "当前延迟较高，请切换香港服务器",
            "reason_en": "REGION-LOCK;MAINLAND-CHINA-ONLY",
            "rule_id": "region-lock",
            "source": "server_geo_policy",
            "rule": {"source_action": "kick"},
        }

        response = await access.check_player_access(
            access.PlayerAccessRequest(
                uid="1000000000001",
                nucleusId=1000000000001,
                playerName="period-test",
                ip="203.0.113.10",
                port=37005,
            ),
            None,
        )

        self.assertEqual(response.reason, "当前延迟较高，请切换香港服务器.")
        self.assertEqual(response.reasonEn, "REGION-LOCK;MAINLAND-CHINA-ONLY.")

    @patch.object(access.player_access_service, "process_online_players_report", new_callable=AsyncMock)
    async def test_online_response_enforces_period_without_duplication(self, report_mock: AsyncMock) -> None:
        report_mock.return_value = {
            "actions": [
                {
                    "uid": "1000000000001",
                    "nucleusId": 1000000000001,
                    "action": "kick",
                    "reason": "キック: ルール違反。",
                    "reasonEn": "Kicked: Rule violation.",
                    "ruleId": "kick-test",
                }
            ]
        }

        response = await access.report_online_players(access.OnlinePlayersRequest(), None)

        self.assertEqual(response.actions[0].reason, "キック: ルール違反。.")
        self.assertEqual(response.actions[0].reasonEn, "Kicked: Rule violation.")

    @patch.object(access.player_access_service, "process_online_players_report", new_callable=AsyncMock)
    async def test_online_request_preserves_player_network_stats(self, report_mock: AsyncMock) -> None:
        report_mock.return_value = {"actions": []}
        payload = access.OnlinePlayersRequest(
            players=[
                access.OnlinePlayer(
                    uid="1000000000001",
                    nucleusId=1000000000001,
                    playerName="network-stats-test",
                    ping=42,
                    loss=3,
                )
            ]
        )

        await access.report_online_players(payload, None)

        report_mock.assert_awaited_once()
        call_args = report_mock.await_args
        self.assertIsNotNone(call_args)
        assert call_args is not None
        self.assertEqual(call_args.kwargs["report"]["players"][0]["ping"], 42)
        self.assertEqual(call_args.kwargs["report"]["players"][0]["loss"], 3)


if __name__ == "__main__":
    unittest.main()
