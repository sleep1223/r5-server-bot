import unittest
from unittest.mock import patch

from fastapi_service.services import milky_service
from fastapi_service.tasks import sync_milky_admins
from shared_lib.config import settings
from shared_lib.models import Player, UserBinding
from tortoise import Tortoise

TORTOISE_TEST_CONFIG = {
    "connections": {"default": "sqlite://:memory:"},
    "apps": {"models": {"models": ["shared_lib.models"], "default_connection": "default"}},
    "use_tz": False,
    "timezone": "Asia/Shanghai",
}


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class _FakeClient:
    payload: object = {}
    last_request: dict | None = None

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, **kwargs: object) -> _FakeResponse:
        type(self).last_request = {"url": url, **kwargs}
        return _FakeResponse(type(self).payload)


class MilkyAdminSyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await Tortoise.init(config=TORTOISE_TEST_CONFIG)
        await Tortoise.generate_schemas()
        self.original_values = (
            settings.milky_api_base_url,
            settings.milky_access_token,
            settings.milky_admin_group_id,
        )
        settings.milky_api_base_url = "http://milky.local/milky/api"
        settings.milky_access_token = "secret-token"
        settings.milky_admin_group_id = 1075088616

    async def asyncTearDown(self) -> None:
        settings.milky_api_base_url, settings.milky_access_token, settings.milky_admin_group_id = self.original_values
        await Tortoise.close_connections()

    async def test_sync_parses_members_and_grants_binding(self) -> None:
        player = await Player.create(nucleus_id=3001, name="milky-player")
        binding = await UserBinding.create(platform="qq", platform_uid="4001", player=player, app_key="app-key-4001")
        _FakeClient.payload = {
            "status": "ok",
            "retcode": 0,
            "data": {"members": [{"user_id": 4001, "role": "member"}, {"user_id": 4001, "role": "admin"}]},
        }

        with patch.object(milky_service.httpx, "AsyncClient", _FakeClient):
            summary = await sync_milky_admins.sync_milky_admins_once()

        await binding.refresh_from_db()
        self.assertTrue(binding.is_admin)
        assert summary is not None and _FakeClient.last_request is not None
        self.assertEqual(summary["members"], 1)
        self.assertEqual(_FakeClient.last_request["url"], "http://milky.local/milky/api/get_group_member_list")
        self.assertEqual(_FakeClient.last_request["headers"], {"Authorization": "Bearer secret-token"})
        self.assertEqual(_FakeClient.last_request["json"], {"group_id": 1075088616, "no_cache": False})

    async def test_sync_rejects_failed_payload_without_writes(self) -> None:
        player = await Player.create(nucleus_id=3002, name="failed-player")
        binding = await UserBinding.create(platform="qq", platform_uid="4002", player=player, app_key="app-key-4002")
        _FakeClient.payload = {"status": "failed", "retcode": -403, "message": "offline"}

        with patch.object(milky_service.httpx, "AsyncClient", _FakeClient):
            with self.assertRaises(ValueError):
                await sync_milky_admins.sync_milky_admins_once()

        await binding.refresh_from_db()
        self.assertFalse(binding.is_admin)

    async def test_send_private_message_uses_text_segment(self) -> None:
        _FakeClient.payload = {"status": "ok", "retcode": 0, "data": {"message_id": 123}}

        with patch.object(milky_service.httpx, "AsyncClient", _FakeClient):
            result = await milky_service.send_private_message(1259332131, "消息内容")

        assert _FakeClient.last_request is not None
        self.assertEqual(result, {"message_id": 123})
        self.assertEqual(_FakeClient.last_request["url"], "http://milky.local/milky/api/send_private_message")
        self.assertEqual(
            _FakeClient.last_request["json"],
            {
                "user_id": 1259332131,
                "message": [{"type": "text", "data": {"text": "消息内容"}}],
            },
        )


if __name__ == "__main__":
    unittest.main()
