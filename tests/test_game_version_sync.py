import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi_service.tasks import sync_game_version
from shared_lib.config import settings


class _FakeResponse:
    text = ""

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    last_url = ""

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, url: str) -> _FakeResponse:
        type(self).last_url = url
        return _FakeResponse()


class GameVersionSyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "launcher_config.toml"
        self.original_values = (
            settings.launcher_config_path,
            settings.launcher_game_version_url,
            settings.launcher_game_version_notify_qq,
        )
        settings.launcher_config_path = str(self.config_path)
        settings.launcher_game_version_url = "https://version.local/version.txt"
        settings.launcher_game_version_notify_qq = 1259332131

    async def asyncTearDown(self) -> None:
        settings.launcher_config_path, settings.launcher_game_version_url, settings.launcher_game_version_notify_qq = self.original_values
        self.temp_dir.cleanup()

    async def test_update_preserves_other_toml_and_sends_private_message(self) -> None:
        original = 'game_version = "v2.6.50-live"\r\n\r\n[announcement]\r\ntitle = "保留内容"\r\n'
        self.config_path.write_text(original, encoding="utf-8", newline="")
        _FakeResponse.text = "2.6.51-live\n"
        send_private_message = AsyncMock()

        with (
            patch.object(sync_game_version.httpx, "AsyncClient", _FakeClient),
            patch.object(sync_game_version, "send_private_message", send_private_message),
        ):
            result = await sync_game_version.sync_game_version_once()

        self.assertEqual(result, ("v2.6.50-live", "2.6.51-live"))
        self.assertEqual(_FakeClient.last_url, "https://version.local/version.txt")
        with self.config_path.open("rb") as file:
            config = tomllib.load(file)
        self.assertEqual(config["game_version"], "2.6.51-live")
        self.assertEqual(config["announcement"]["title"], "保留内容")
        with self.config_path.open("r", encoding="utf-8", newline="") as file:
            updated = file.read()
        self.assertIn("\r\n", updated)
        send_private_message.assert_awaited_once()
        await_args = send_private_message.await_args
        assert await_args is not None
        self.assertEqual(await_args.args[0], 1259332131)
        self.assertIn("v2.6.50-live -> 2.6.51-live", await_args.args[1])

    async def test_optional_v_prefix_does_not_trigger_update(self) -> None:
        original = 'game_version = "v2.6.51-live"\n'
        self.config_path.write_text(original, encoding="utf-8", newline="")
        _FakeResponse.text = "2.6.51-live"
        send_private_message = AsyncMock()

        with (
            patch.object(sync_game_version.httpx, "AsyncClient", _FakeClient),
            patch.object(sync_game_version, "send_private_message", send_private_message),
        ):
            result = await sync_game_version.sync_game_version_once()

        self.assertIsNone(result)
        with self.config_path.open("r", encoding="utf-8", newline="") as file:
            unchanged = file.read()
        self.assertEqual(unchanged, original)
        send_private_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
