import unittest

from fastapi_service.services import admin_service
from shared_lib.models import BanRecord, Player, PlayerAccessOperation
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
        await Tortoise.init(config=TORTOISE_TEST_CONFIG)
        await Tortoise.generate_schemas()

    async def asyncTearDown(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
