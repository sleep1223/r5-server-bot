from __future__ import annotations

import asyncio
import time
from contextlib import suppress

import httpx
from nonebot import get_driver, get_plugin_config, logger
from nonebot.adapters.onebot.v11 import Bot  # noqa: TC002
from nonebot.exception import FinishedException

from ..api_client import api_client
from ..config import Config
from .common import on_command, r5_service

plugin_config = get_plugin_config(Config)
driver = get_driver()

admin_group_svc = r5_service.create_subservice("admin_group")
refresh_admin_group_cmd = on_command("刷新管理员列表", aliases={"刷新管理群列表", "刷新管理列表"}, priority=4, block=True)
_sync_tasks: dict[str, asyncio.Task[None]] = {}
_admin_group_cache = {
    "members": set(),
    "last_refresh_at": None,
}
_cache_lock = asyncio.Lock()


def _enabled() -> bool:
    return plugin_config.r5_admin_group_id > 0


def _cache_seconds() -> int:
    return max(60, int(plugin_config.r5_admin_group_cache_seconds))


def _normalize_qq(value: object) -> str:
    return str(value or "").strip()


def _configured_admin_qqs() -> set[str]:
    configured = {_normalize_qq(item) for item in plugin_config.r5_admin_group_grant_excluded_qqs}
    configured.discard("")
    return configured


def _should_skip_configured_admin(qq: str) -> bool:
    return qq in _configured_admin_qqs()


async def _cached_member_qqs() -> tuple[set[str], int | None]:
    async with _cache_lock:
        members = _admin_group_cache["members"]
        last_refresh_at = _admin_group_cache["last_refresh_at"]
        return set(members), last_refresh_at


def _cache_is_fresh(members: set[str], last_refresh: int | None) -> bool:
    return bool(members and last_refresh and time.time() - last_refresh < _cache_seconds())


async def _store_member_qqs(members: set[str]) -> None:
    async with _cache_lock:
        cached_members = _admin_group_cache["members"]
        cached_members.clear()
        cached_members.update(members)
        _admin_group_cache["last_refresh_at"] = int(time.time())


async def _fetch_group_member_qqs(bot: Bot) -> set[str]:
    raw_members = await bot.call_api("get_group_member_list", group_id=plugin_config.r5_admin_group_id)
    if not isinstance(raw_members, list):
        logger.warning(f"get_group_member_list 返回格式异常: {type(raw_members)!r}")
        return set()

    members: set[str] = set()
    for item in raw_members:
        if not isinstance(item, dict):
            continue
        qq = _normalize_qq(item.get("user_id"))
        if qq:
            members.add(qq)
    return members


async def get_admin_group_member_qqs(bot: Bot, *, force_refresh: bool = False) -> set[str]:
    if not _enabled():
        return set()

    cached_members, last_refresh = await _cached_member_qqs()
    if not force_refresh and _cache_is_fresh(cached_members, last_refresh):
        return cached_members

    members = await _fetch_group_member_qqs(bot)
    if members:
        await _store_member_qqs(members)
        return members

    return cached_members


async def _grant_admin_for_qq(qq: str) -> str:
    if _should_skip_configured_admin(qq):
        return "skipped_configured"

    try:
        resp = await api_client.grant_admin_by_platform(platform="qq", platform_uid=qq, timeout=5.0)
        req = resp.json()
    except httpx.RequestError as e:
        logger.warning(f"同步管理群管理员权限失败: qq={qq}, error={e}")
        return "failed"
    except ValueError as e:
        logger.warning(f"同步管理群管理员权限返回非 JSON: qq={qq}, error={e}")
        return "failed"

    if req.get("code") != "0000":
        logger.warning(f"同步管理群管理员权限失败: qq={qq}, msg={req.get('msg', '未知错误')}")
        return "failed"

    data = req.get("data") or {}
    status = _normalize_qq(data.get("status"))
    return status or "failed"


def _count_status(summary: dict[str, int], status: str) -> None:
    if status == "granted":
        summary["granted"] += 1
    elif status == "already_admin":
        summary["already_admin"] += 1
    elif status == "not_bound":
        summary["not_bound"] += 1
    elif status in {"skipped_super_admin", "skipped_configured"}:
        summary["skipped"] += 1
    else:
        summary["failed"] += 1


async def refresh_and_grant_admins(bot: Bot, *, force_refresh: bool = False) -> dict[str, int]:
    members = await get_admin_group_member_qqs(bot, force_refresh=force_refresh)
    summary = {
        "members": len(members),
        "granted": 0,
        "already_admin": 0,
        "not_bound": 0,
        "skipped": 0,
        "failed": 0,
    }
    if not members:
        return summary

    semaphore = asyncio.Semaphore(10)

    async def grant_one(qq: str) -> str:
        async with semaphore:
            return await _grant_admin_for_qq(qq)

    statuses = await asyncio.gather(*(grant_one(qq) for qq in sorted(members)))
    for status in statuses:
        _count_status(summary, status)
    return summary


async def grant_admin_if_group_member(bot: Bot, qq: object) -> str:
    normalized_qq = _normalize_qq(qq)
    if not normalized_qq or _should_skip_configured_admin(normalized_qq):
        return "skipped_configured"

    try:
        members = await get_admin_group_member_qqs(bot)
    except Exception as e:
        logger.warning(f"绑定后同步管理群管理员权限失败: qq={normalized_qq}, error={e}")
        return "failed"

    if normalized_qq not in members:
        return "not_group_member"
    return await _grant_admin_for_qq(normalized_qq)


def _format_refresh_summary(summary: dict[str, int]) -> str:
    return (
        "✅ 管理员列表已刷新\n"
        f"群号: {plugin_config.r5_admin_group_id}\n"
        f"缓存 QQ: {summary['members']} 个\n"
        f"新增授权: {summary['granted']}\n"
        f"已是管理员: {summary['already_admin']}\n"
        f"未绑定: {summary['not_bound']}\n"
        f"已跳过: {summary['skipped']}\n"
        f"失败: {summary['failed']}"
    )


async def _daily_sync_loop(bot: Bot) -> None:
    while True:
        try:
            summary = await refresh_and_grant_admins(bot)
            logger.info(f"管理群管理员权限同步完成: {summary}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"管理群管理员权限同步异常: {e}")
        await asyncio.sleep(_cache_seconds())


@driver.on_bot_connect
async def _start_admin_group_sync(bot: Bot) -> None:
    if not _enabled():
        return
    task = _sync_tasks.get(bot.self_id)
    if task and not task.done():
        return
    _sync_tasks[bot.self_id] = asyncio.create_task(_daily_sync_loop(bot))


@driver.on_bot_disconnect
async def _stop_admin_group_sync(bot: Bot) -> None:
    task = _sync_tasks.pop(bot.self_id, None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@driver.on_shutdown
async def _close_admin_group_resources() -> None:
    for task in _sync_tasks.values():
        task.cancel()
    for task in _sync_tasks.values():
        with suppress(asyncio.CancelledError):
            await task
    _sync_tasks.clear()


@refresh_admin_group_cmd.handle()
@admin_group_svc.patch_handler()
async def handle_refresh_admin_group(bot: Bot) -> None:
    if not _enabled():
        await refresh_admin_group_cmd.finish("⚠️ 管理群自动授权未启用")

    try:
        summary = await refresh_and_grant_admins(bot, force_refresh=True)
        await refresh_admin_group_cmd.finish(_format_refresh_summary(summary))
    except FinishedException:
        raise
    except Exception as e:
        logger.exception(f"刷新管理员列表失败: {e}")
        await refresh_admin_group_cmd.finish(f"❌ 刷新管理员列表失败: {e}")
