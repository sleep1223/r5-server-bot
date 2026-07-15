import unittest

from fastapi_service.tasks.fetch_servers import _upsert_servers_from_raw
from shared_lib.models import Server
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


class FetchServersUpsertTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await Tortoise.init(config=TORTOISE_TEST_CONFIG)
        await Tortoise.generate_schemas()

    async def asyncTearDown(self) -> None:
        await Tortoise.close_connections()

    async def test_missing_ip_preserves_existing_address(self) -> None:
        await Server.create(
            server_id="address-owner",
            host="172.93.101.24",
            port=37015,
            name="Address Owner",
            has_status=True,
        )
        await Server.create(
            server_id="bf44e8785a8e416c44ed1ef7454d0e14",
            host="172.93.101.24",
            port=37016,
            name="Old Name",
            has_status=False,
        )

        await _upsert_servers_from_raw([
            {
                "serverId": "bf44e8785a8e416c44ed1ef7454d0e14",
                "name": "Updated Name",
                "region": "US",
                "playerCount": 3,
            }
        ])

        server = await Server.get(server_id="bf44e8785a8e416c44ed1ef7454d0e14")
        self.assertEqual((server.host, server.port), ("172.93.101.24", 37016))
        self.assertEqual(server.name, "Updated Name")
        self.assertEqual(server.player_count, 3)
        self.assertTrue(server.has_status)


if __name__ == "__main__":
    unittest.main()
