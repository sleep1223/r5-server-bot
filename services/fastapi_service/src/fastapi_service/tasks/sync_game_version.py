import asyncio
import os
import re
import stat
import tempfile
import tomllib
from pathlib import Path

import httpx
from loguru import logger
from shared_lib.config import settings

from fastapi_service.services.milky_service import send_private_message

_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_GAME_VERSION_LINE_PATTERN = re.compile(
    r'^(?P<prefix>[ \t]*game_version[ \t]*=[ \t]*)"(?:[^"\\]|\\.)*"(?P<suffix>[ \t]*(?:#.*)?)(?P<newline>\r?\n|$)',
    re.MULTILINE,
)


def _normalized_version(version: str) -> str:
    normalized = version.strip()
    return normalized[1:] if normalized.startswith("v") else normalized


def _read_game_version(path: Path) -> str:
    with path.open("rb") as file:
        config = tomllib.load(file)
    return str(config.get("game_version") or "").strip()


def _replace_game_version(path: Path, version: str) -> None:
    with path.open("r", encoding="utf-8", newline="") as file:
        content = file.read()

    updated, count = _GAME_VERSION_LINE_PATTERN.subn(
        lambda match: f'{match.group("prefix")}"{version}"{match.group("suffix")}{match.group("newline")}',
        content,
    )
    if count != 1:
        raise ValueError(f"launcher 配置中的 game_version 数量异常: {count}")

    temp_path: Path | None = None
    try:
        file_descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as file:
            file.write(updated)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_path, stat.S_IMODE(path.stat().st_mode))
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


async def _fetch_latest_game_version() -> str:
    url = settings.launcher_game_version_url.strip()
    if not url:
        raise ValueError("未配置游戏版本地址")

    timeout = max(float(settings.launcher_game_version_request_timeout_seconds), 1.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()

    version = response.text.strip()
    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"远端游戏版本号格式无效: {version!r}")
    return version


async def sync_game_version_once() -> tuple[str, str] | None:
    latest_version = await _fetch_latest_game_version()
    config_path = Path(settings.launcher_config_path)
    current_version = _read_game_version(config_path)
    if _normalized_version(current_version) == _normalized_version(latest_version):
        return None

    _replace_game_version(config_path, latest_version)
    logger.info(f"游戏版本已更新: {current_version or '未配置'} -> {latest_version}")

    notify_qq = int(settings.launcher_game_version_notify_qq)
    if notify_qq <= 0:
        logger.warning("游戏版本已更新，但未配置 QQ 私聊提醒接收人")
    else:
        message = f"R5 Reloaded 游戏版本已更新：{current_version or '未配置'} -> {latest_version}，launcher_config.toml 已同步。"
        try:
            await send_private_message(notify_qq, message)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(f"游戏版本已更新，但 QQ 私聊提醒发送失败: {exc}")
        except Exception:
            logger.exception("游戏版本已更新，但 QQ 私聊提醒发送异常")

    return current_version, latest_version


async def sync_game_version_task() -> None:
    url = settings.launcher_game_version_url.strip()
    if not url:
        logger.info("游戏版本同步未启用")
        return

    interval = max(int(settings.launcher_game_version_fetch_interval), 60)
    logger.info(f"游戏版本同步任务已启动: url={url}, interval={interval}s")
    while True:
        try:
            await sync_game_version_once()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            logger.warning(f"游戏版本同步失败: {exc}")
        except Exception:
            logger.exception("游戏版本同步异常")
        await asyncio.sleep(interval)
