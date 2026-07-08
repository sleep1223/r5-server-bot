import traceback
from datetime import datetime
from typing import Any

import httpx
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import on_command, r5_service

apex_service = r5_service.create_subservice("apex")
apex_map_service = apex_service.create_subservice("map")
apex_predator_service = apex_service.create_subservice("predator")

apex_map_cmd = on_command("查地图", aliases={"地图", "apex地图", "apex map", "map"}, priority=5, block=True)
apex_predator_cmd = on_command("查顶猎", aliases={"顶猎", "顶猎分数", "apex顶猎", "predator"}, priority=5, block=True)

_MAP_MODE_LABELS = {
    "battle_royale": "大逃杀",
    "ranked": "排位赛",
    "ltm": "混录带",
}

_PLATFORM_LABELS = {
    "PC": "PC",
    "PS4": "PlayStation",
    "X1": "Xbox",
    "SWITCH": "Switch",
}

_PLATFORM_ALIASES = {
    "pc": "PC",
    "电脑": "PC",
    "ps": "PS4",
    "ps4": "PS4",
    "ps5": "PS4",
    "playstation": "PS4",
    "xbox": "X1",
    "x1": "X1",
    "switch": "SWITCH",
    "ns": "SWITCH",
}


def _unwrap_response(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("code") != "0000":
        raise RuntimeError(payload.get("msg") or "后端返回异常")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TypeError("后端返回数据格式异常")
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data


def _format_updated_at(value: str | None) -> str:
    if not value:
        return "等待缓存"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.astimezone().strftime("%m-%d %H:%M")


def _display_text(value: Any, zh_value: Any = None) -> str:
    text = zh_value if zh_value not in (None, "") else value
    return str(text) if text not in (None, "") else "未知"


def _format_map_mode(mode_key: str, mode: dict[str, Any]) -> str:
    current = mode.get("current") if isinstance(mode.get("current"), dict) else {}
    next_map = mode.get("next") if isinstance(mode.get("next"), dict) else {}
    current_name = _display_text(current.get("map"), current.get("map_zh"))
    next_name = _display_text(next_map.get("map"), next_map.get("map_zh"))
    remaining = current.get("remainingTimer") or "未知"
    return (
        f"🎮 {_MAP_MODE_LABELS.get(mode_key, mode_key)}\n"
        f"  当前: {current_name}\n"
        f"  下张: {next_name}\n"
        f"  剩余: {remaining}"
    )


def _format_map_message(cache_payload: dict[str, Any]) -> str:
    data = cache_payload.get("data") if isinstance(cache_payload.get("data"), dict) else {}
    lines = [
        "🗺️ Apex 地图轮换",
        f"🕒 缓存: {_format_updated_at(cache_payload.get('updated_at'))}",
        "",
    ]
    for key in ("battle_royale", "ranked", "ltm"):
        mode = data.get(key) if isinstance(data.get(key), dict) else {}
        lines.append(_format_map_mode(key, mode))
        lines.append("")
    lines.append("📡 数据来自 Apex Legends Status")
    return "\n".join(lines).strip()


def _parse_platform_filter(text: str) -> str | None:
    raw = text.strip().lower()
    return _PLATFORM_ALIASES.get(raw)


def _format_predator_row(platform: str, info: dict[str, Any]) -> str:
    return (
        f"🏹 {_PLATFORM_LABELS.get(platform, platform)}\n"
        f"  底分: {info.get('val', 0)} RP\n"
        f"  大师+顶猎: {info.get('total_masters', 0)}"
    )


def _format_predator_message(cache_payload: dict[str, Any], platform_filter: str | None = None) -> str:
    data = cache_payload.get("data") if isinstance(cache_payload.get("data"), dict) else {}
    lines = [
        "👑 Apex 顶尖猎杀者分数线",
        f"🕒 缓存: {_format_updated_at(cache_payload.get('updated_at'))}",
        "",
    ]
    platforms = [platform_filter] if platform_filter else ["PC", "PS4", "X1", "SWITCH"]
    for platform in platforms:
        info = data.get(platform) if isinstance(data.get(platform), dict) else None
        if not info:
            continue
        lines.append(_format_predator_row(platform, info))
        lines.append("")
    lines.append("📡 数据来自 Apex Legends Status")
    return "\n".join(lines).strip()


@apex_map_cmd.handle()
@apex_map_service.patch_handler()
async def handle_apex_map() -> None:
    try:
        resp = await api_client.get_apex_map_rotation(timeout=5.0)
        if resp.status_code != 200:
            await apex_map_cmd.finish(f"❌ 查询失败: HTTP {resp.status_code}")
        cache_payload = _unwrap_response(resp.json())
        await apex_map_cmd.finish(_format_map_message(cache_payload))
    except FinishedException:
        raise
    except httpx.RequestError as exc:
        await apex_map_cmd.finish(f"❌ 网络请求错误: {exc}")
    except Exception as exc:
        traceback.print_exc()
        await apex_map_cmd.finish(f"❌ 查询出错: {exc}")


@apex_predator_cmd.handle()
@apex_predator_service.patch_handler()
async def handle_apex_predator(args: Message = CommandArg()) -> None:
    platform_filter = _parse_platform_filter(args.extract_plain_text())
    try:
        resp = await api_client.get_apex_predator(timeout=5.0)
        if resp.status_code != 200:
            await apex_predator_cmd.finish(f"❌ 查询失败: HTTP {resp.status_code}")
        cache_payload = _unwrap_response(resp.json())
        await apex_predator_cmd.finish(_format_predator_message(cache_payload, platform_filter))
    except FinishedException:
        raise
    except httpx.RequestError as exc:
        await apex_predator_cmd.finish(f"❌ 网络请求错误: {exc}")
    except Exception as exc:
        traceback.print_exc()
        await apex_predator_cmd.finish(f"❌ 查询出错: {exc}")
