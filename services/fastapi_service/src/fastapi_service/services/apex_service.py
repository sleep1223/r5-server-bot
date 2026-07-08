from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from loguru import logger
from shared_lib.config import settings
from shared_lib.models import ApexPlayerStatsSnapshot

from .apex_translations import translate_apex_text

Platform = Literal["PC", "PS4", "X1", "SWITCH"]
CachedResource = Literal["map_rotation", "server_status", "predator"]

VALID_PLATFORMS: set[str] = {"PC", "PS4", "X1", "SWITCH"}
DATA_CREDIT = "Data provided by Apex Legends Status"

_SERVER_SECTIONS: dict[str, tuple[str, str]] = {
    "Origin_login": ("Origin Login", "Origin 登录"),
    "EA_novafusion": ("EA Novafusion", "EA 融合"),
    "EA_accounts": ("EA Accounts", "EA 账户"),
    "ApexOauth_Crossplay": ("Apex Crossplay Auth", "Apex 跨平台验证"),
    "selfCoreTest": ("Self Core Tests", "自我核心测试"),
    "otherPlatforms": ("Other Platforms", "其他平台"),
}
_SERVER_REGIONS: dict[str, tuple[str, str]] = {
    "EU-West": ("EU West", "欧盟西部"),
    "EU-East": ("EU East", "欧盟东部"),
    "US-West": ("US West", "美国西部"),
    "US-Central": ("US Central", "美国中部"),
    "US-East": ("US East", "美国东部"),
    "SouthAmerica": ("South America", "南美洲"),
    "Asia": ("Asia", "亚洲"),
}
_SELF_CORE_TESTS: dict[str, tuple[str, str]] = {
    "Status-website": ("Status Website", "网站状态"),
    "Stats-API": ("Stats API", "统计 API"),
    "Overflow-#1": ("Overflow #1", "溢出 #1"),
    "Overflow-#2": ("Overflow #2", "溢出 #2"),
    "Origin-API": ("Origin API", "Origin API"),
    "Playstation-API": ("Playstation API", "Playstation API"),
    "Xbox-API": ("Xbox API", "Xbox API"),
}
_OTHER_PLATFORMS: dict[str, tuple[str, str]] = {
    "Playstation-Network": ("Playstation Network", "Playstation Network"),
    "Xbox-Live": ("Xbox Live", "Xbox Live"),
}


class ApexServiceError(Exception):
    def __init__(self, message: str, *, status_code: int = 502, api_status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.api_status_code = api_status_code


@dataclass
class ApexCacheEntry:
    data: Any = None
    raw: Any = None
    updated_at: datetime | None = None
    error: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "data": self.data,
            "raw": self.raw,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "error": self.error,
            "credit": DATA_CREDIT,
        }


class ApexDataCache:
    def __init__(self) -> None:
        self._entries: dict[str, ApexCacheEntry] = {}
        self._lock = asyncio.Lock()

    async def set(self, key: CachedResource, *, data: Any, raw: Any = None, error: str | None = None) -> None:
        async with self._lock:
            self._entries[key] = ApexCacheEntry(
                data=data,
                raw=raw,
                updated_at=datetime.now(timezone.utc),
                error=error,
            )

    async def set_error(self, key: CachedResource, error: str) -> None:
        async with self._lock:
            entry = self._entries.get(key, ApexCacheEntry())
            entry.error = error
            self._entries[key] = entry

    async def get(self, key: CachedResource) -> ApexCacheEntry | None:
        async with self._lock:
            return self._entries.get(key)


apex_cache = ApexDataCache()


def normalize_platform(platform: str) -> Platform:
    normalized = platform.strip().upper()
    if normalized not in VALID_PLATFORMS:
        raise ApexServiceError(
            f"平台参数错误，请输入 {', '.join(sorted(VALID_PLATFORMS))}",
            status_code=400,
        )
    return normalized  # type: ignore[return-value]


def _api_key() -> str:
    key = (settings.apex_api_key or "").strip()
    if not key:
        raise ApexServiceError("未配置 apex_api_key", status_code=500)
    return key


def _base_url() -> str:
    return (settings.apex_api_url or "https://api.apexlegendsstatus.com").rstrip("/")


async def _fetch(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"auth": _api_key()}
    if params:
        payload.update(params)
    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            trust_env=False,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        ) as client:
            response = await client.get(f"{_base_url()}/{endpoint.lstrip('/')}", params=payload)
    except httpx.HTTPError as exc:
        raise ApexServiceError("查询 Apex API 失败: 网络请求错误") from exc

    if response.status_code != 200:
        message = response.text.strip() or f"Apex API 返回 HTTP {response.status_code}"
        raise ApexServiceError(message, api_status_code=response.status_code)

    try:
        data = response.json()
    except ValueError as exc:
        raise ApexServiceError("查询 Apex API 失败: 返回数据不是 JSON") from exc

    if not isinstance(data, dict):
        raise ApexServiceError("查询 Apex API 失败: 返回数据格式异常")
    return data


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _extract_uid(data: Any) -> str | None:
    if isinstance(data, dict):
        direct = _first_present(data, ("uid", "id", "nucleusId", "nucleus_id"))
        if direct is not None:
            return str(direct)
        for value in data.values():
            found = _extract_uid(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _extract_uid(item)
            if found:
                return found
    elif isinstance(data, (int, str)):
        text = str(data).strip()
        if text.isdigit():
            return text
    return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _player_summary(raw: dict[str, Any]) -> dict[str, Any]:
    global_value = raw.get("global")
    realtime_value = raw.get("realtime")
    global_data: dict[str, Any] = global_value if isinstance(global_value, dict) else {}
    realtime_data: dict[str, Any] = realtime_value if isinstance(realtime_value, dict) else {}

    rank_value = global_data.get("rank")
    bans_value = global_data.get("bans")
    rank_data: dict[str, Any] = rank_value if isinstance(rank_value, dict) else {}
    bans_data: dict[str, Any] = bans_value if isinstance(bans_value, dict) else {}

    rank_name = rank_data.get("rankName")
    selected_legend = realtime_data.get("selectedLegend")
    lobby_state = realtime_data.get("lobbyState")
    current_state = realtime_data.get("currentState")
    current_state_text = realtime_data.get("currentStateAsText")

    return {
        "uid": global_data.get("uid"),
        "name": global_data.get("name"),
        "platform": global_data.get("platform"),
        "level": _as_int(global_data.get("level")),
        "to_next_level_percent": global_data.get("toNextLevelPercent"),
        "rank_score": _as_int(rank_data.get("rankScore")),
        "rank_name": rank_name,
        "rank_name_zh": translate_apex_text(rank_name),
        "rank_div": rank_data.get("rankDiv"),
        "rank_img": rank_data.get("rankImg"),
        "selected_legend": selected_legend,
        "selected_legend_zh": translate_apex_text(selected_legend),
        "lobby_state": lobby_state,
        "lobby_state_zh": translate_apex_text(lobby_state),
        "is_online": realtime_data.get("isOnline"),
        "can_join": realtime_data.get("canJoin"),
        "party_full": realtime_data.get("partyFull"),
        "current_state": current_state,
        "current_state_zh": translate_apex_text(current_state),
        "current_state_text": current_state_text,
        "current_state_text_zh": translate_apex_text(current_state_text),
        "ban_is_active": bans_data.get("isActive"),
        "ban_remaining_seconds": bans_data.get("remainingSeconds"),
        "ban_reason": bans_data.get("last_banReason"),
    }


def _comparison(current: dict[str, Any], previous: ApexPlayerStatsSnapshot | None) -> dict[str, Any]:
    if previous is None:
        return {"has_previous": False, "changes": {}, "previous": None}

    changes: dict[str, Any] = {}
    level_diff = _as_int(current.get("level")) - previous.level
    if level_diff:
        changes["level"] = level_diff

    score_diff = _as_int(current.get("rank_score")) - previous.rank_score
    if score_diff:
        changes["rank_score"] = score_diff

    current_rank = {
        "rank_name": current.get("rank_name") or "",
        "rank_div": current.get("rank_div"),
    }
    previous_rank = {
        "rank_name": previous.rank_name or "",
        "rank_div": previous.rank_div,
    }
    if current_rank != previous_rank:
        changes["rank"] = {"from": previous_rank, "to": current_rank}

    return {
        "has_previous": True,
        "changes": changes,
        "previous": {
            "level": previous.level,
            "rank_score": previous.rank_score,
            "rank_name": previous.rank_name,
            "rank_div": previous.rank_div,
            "created_at": previous.created_at.isoformat() if previous.created_at else None,
        },
    }


async def resolve_uid(player_name: str, platform: str = "PC") -> dict[str, Any]:
    player = player_name.strip()
    if not player:
        raise ApexServiceError("请输入玩家名称", status_code=400)
    normalized_platform = normalize_platform(platform)
    raw = await _fetch("nametouid", {"player": player, "platform": normalized_platform})
    uid = _extract_uid(raw)
    if not uid:
        raise ApexServiceError("Apex API 未返回玩家 UID")
    return {
        "player_name": player,
        "platform": normalized_platform,
        "uid": uid,
        "raw": raw,
        "credit": DATA_CREDIT,
    }


async def get_player_stats(
    *,
    player_name: str | None = None,
    uid: str | None = None,
    platform: str = "PC",
    resolve_uid_first: bool = False,
    save_snapshot: bool = True,
) -> dict[str, Any]:
    normalized_platform = normalize_platform(platform)
    clean_uid = str(uid or "").strip()
    clean_player = str(player_name or "").strip()
    resolved: dict[str, Any] | None = None

    if not clean_uid and clean_player and resolve_uid_first:
        resolved = await resolve_uid(clean_player, normalized_platform)
        clean_uid = str(resolved["uid"])

    if clean_uid:
        raw = await _fetch("bridge", {"uid": clean_uid, "platform": normalized_platform})
        query = {"uid": clean_uid, "platform": normalized_platform}
    elif clean_player:
        raw = await _fetch("bridge", {"player": clean_player, "platform": normalized_platform})
        query = {"player_name": clean_player, "platform": normalized_platform}
    else:
        raise ApexServiceError("请提供 player_name 或 uid", status_code=400)

    summary = _player_summary(raw)
    snapshot_uid = str(summary.get("uid") or clean_uid or "").strip()
    snapshot_platform = str(summary.get("platform") or normalized_platform)
    comparison: dict[str, Any] = {"has_previous": False, "changes": {}, "previous": None}
    snapshot_id: int | None = None

    if save_snapshot and snapshot_uid:
        previous = await ApexPlayerStatsSnapshot.filter(uid=snapshot_uid, platform=snapshot_platform).order_by("-created_at").first()
        comparison = _comparison(summary, previous)
        snapshot = await ApexPlayerStatsSnapshot.create(
            uid=snapshot_uid,
            player_name=str(summary.get("name") or clean_player or snapshot_uid),
            platform=snapshot_platform,
            level=_as_int(summary.get("level")),
            rank_score=_as_int(summary.get("rank_score")),
            rank_name=str(summary.get("rank_name") or ""),
            rank_div=_as_int(summary.get("rank_div")) if summary.get("rank_div") is not None else None,
            payload=raw,
        )
        snapshot_id = snapshot.id

    return {
        "query": query,
        "resolved": resolved,
        "summary": summary,
        "comparison": comparison,
        "snapshot_id": snapshot_id,
        "raw": raw,
        "credit": DATA_CREDIT,
    }


async def get_player_history(
    *,
    player_name: str | None = None,
    uid: str | None = None,
    platform: str = "PC",
    limit: int = 20,
    resolve_uid_first: bool = False,
) -> dict[str, Any]:
    normalized_platform = normalize_platform(platform)
    clean_uid = str(uid or "").strip()
    clean_player = str(player_name or "").strip()
    resolved: dict[str, Any] | None = None

    if not clean_uid and clean_player and resolve_uid_first:
        resolved = await resolve_uid(clean_player, normalized_platform)
        clean_uid = str(resolved["uid"])

    query = ApexPlayerStatsSnapshot.filter(platform=normalized_platform)
    if clean_uid:
        query = query.filter(uid=clean_uid)
    elif clean_player:
        query = query.filter(player_name__iexact=clean_player)
    else:
        raise ApexServiceError("请提供 player_name 或 uid", status_code=400)

    rows = await query.order_by("-created_at").limit(max(1, min(limit, 100)))
    chronological = list(reversed(rows))
    items: list[dict[str, Any]] = []
    previous: ApexPlayerStatsSnapshot | None = None
    for row in chronological:
        current = {
            "level": row.level,
            "rank_score": row.rank_score,
            "rank_name": row.rank_name,
            "rank_div": row.rank_div,
        }
        items.append({
            "id": row.id,
            "uid": row.uid,
            "player_name": row.player_name,
            "platform": row.platform,
            "level": row.level,
            "rank_score": row.rank_score,
            "rank_name": row.rank_name,
            "rank_div": row.rank_div,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "comparison": _comparison(current, previous),
        })
        previous = row

    return {
        "query": {
            "uid": clean_uid or None,
            "player_name": clean_player or None,
            "platform": normalized_platform,
        },
        "resolved": resolved,
        "items": list(reversed(items)),
        "total_returned": len(items),
    }


async def fetch_map_rotation() -> dict[str, Any]:
    raw = await _fetch("maprotation", {"version": "2"})

    def with_translated_map(mode: Any) -> dict[str, Any]:
        mode_data = mode if isinstance(mode, dict) else {}
        result: dict[str, Any] = {}
        for key in ("current", "next"):
            entry_value = mode_data.get(key)
            entry: dict[str, Any] = dict(entry_value) if isinstance(entry_value, dict) else {}
            entry["map_zh"] = translate_apex_text(entry.get("map"))
            entry["eventName_zh"] = translate_apex_text(entry.get("eventName"))
            result[key] = entry
        return result

    data = {
        "battle_royale": with_translated_map(raw.get("battle_royale")),
        "ranked": with_translated_map(raw.get("ranked")),
        "ltm": with_translated_map(raw.get("ltm")),
    }
    return {"data": data, "raw": raw}


def _traverse_server_sections(raw: dict[str, Any]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for section_key, (section_name, section_name_zh) in _SERVER_SECTIONS.items():
        rows: list[dict[str, Any]] = []
        section_data = raw.get(section_key, {})
        if not isinstance(section_data, dict):
            section_data = {}

        if section_key == "selfCoreTest":
            pairs = _SELF_CORE_TESTS.items()
        elif section_key == "otherPlatforms":
            pairs = _OTHER_PLATFORMS.items()
        else:
            pairs = _SERVER_REGIONS.items()

        for key, (name, name_zh) in pairs:
            entry = section_data.get(key, {})
            if not isinstance(entry, dict):
                entry = {}
            rows.append({
                "name": name,
                "name_zh": name_zh,
                "key": key,
                "status": entry.get("Status", ""),
                "status_zh": translate_apex_text(entry.get("Status", "")),
                "response_time": entry.get("ResponseTime", -1),
            })

        sections.append({
            "section_name": section_name,
            "section_name_zh": section_name_zh,
            "section_key": section_key,
            "rows": rows,
        })
    return sections


async def fetch_server_status() -> dict[str, Any]:
    raw = await _fetch("servers")
    return {"data": _traverse_server_sections(raw), "raw": raw}


async def fetch_predator() -> dict[str, Any]:
    raw = await _fetch("predator")
    rp = raw.get("RP") if isinstance(raw.get("RP"), dict) else {}
    platforms = {
        "PC": "PC 端",
        "PS4": "PS4/5 端",
        "X1": "Xbox 端",
        "SWITCH": "Switch 端",
    }
    data: dict[str, Any] = {}
    for platform, name in platforms.items():
        item = rp.get(platform, {}) if isinstance(rp, dict) else {}
        if not isinstance(item, dict):
            item = {}
        data[platform] = {
            "name": name,
            "val": item.get("val", 0),
            "total_masters": item.get("totalMastersAndPreds", 0),
        }
    return {"data": data, "raw": None}


async def refresh_cached_resource(resource: CachedResource) -> ApexCacheEntry:
    fetchers = {
        "map_rotation": fetch_map_rotation,
        "server_status": fetch_server_status,
        "predator": fetch_predator,
    }
    payload = await fetchers[resource]()
    await apex_cache.set(resource, data=payload["data"], raw=payload["raw"])
    entry = await apex_cache.get(resource)
    if entry is None:
        raise ApexServiceError("缓存写入失败")
    return entry


async def refresh_all_cached_resources() -> None:
    for resource in ("map_rotation", "server_status", "predator"):
        try:
            await refresh_cached_resource(resource)  # type: ignore[arg-type]
        except Exception as exc:
            logger.warning(f"Apex {resource} 缓存刷新失败: {exc}")
            await apex_cache.set_error(resource, str(exc))


async def get_cached_resource(resource: CachedResource, *, refresh_if_empty: bool = False) -> dict[str, Any]:
    entry = await apex_cache.get(resource)
    if entry is None and refresh_if_empty:
        entry = await refresh_cached_resource(resource)
    if entry is None:
        raise ApexServiceError("缓存尚未初始化", status_code=503)
    return entry.as_payload()
