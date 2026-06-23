from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Literal

from shared_lib.models import Player, Server
from tortoise.expressions import Q

from fastapi_service.core.cache import server_cache
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error
from fastapi_service.core.utils import CN_TZ, parse_short_name
from fastapi_service.services import player_access_service


def _looks_like_server_address(value: object | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False

    host = text
    if text.startswith("[") and "]" in text:
        host = text[1 : text.index("]")]
    elif ":" in text:
        host_part, _, port_part = text.rpartition(":")
        if port_part.isdigit():
            host = host_part

    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


async def _resolve_server_display_by_address(
    server_host: object | None,
    server_port: object | None,
) -> tuple[str | None, str | None]:
    host = str(server_host or "").strip()
    if not host:
        return None, None
    try:
        port = int(str(server_port or 0))
    except ValueError:
        return None, None
    if port <= 0:
        return None, None

    server = await Server.filter(host=host, port=port).first()
    if server is None:
        return None, None

    full_name = server.name or server.short_name
    short_name = server.short_name or parse_short_name(full_name or "")
    return full_name, short_name


async def get_player_by_identifier(identifier: int | str, require_nucleus_id: bool = True) -> tuple[Player, None] | tuple[None, dict]:
    identifier_text = str(identifier).strip()
    filter_q = Q(Q(nucleus_hash__iexact=identifier_text) | Q(name__iexact=identifier_text))
    if identifier_text.isdigit():
        filter_q |= Q(nucleus_id=int(identifier_text))

    player = await Player.filter(filter_q).first()
    if not player:
        return None, error(ErrorCode.PLAYER_NOT_FOUND, msg=f"未找到玩家 {identifier}")

    if require_nucleus_id and not player.nucleus_id:
        return None, error(ErrorCode.PLAYER_NO_NUCLEUS_ID, msg=f"玩家 {identifier} 没有 nucleus_id")

    return player, None


def get_online_location(player: Player) -> tuple[dict | None, dict | None]:
    if not player.nucleus_id:
        return None, error(ErrorCode.PLAYER_NOT_ONLINE, msg=f"玩家 {player.name} 不在线")

    loc = server_cache.get_online_location(player.nucleus_id)
    if loc:
        return loc, None
    return None, error(ErrorCode.PLAYER_NOT_ONLINE, msg=f"玩家 {player.name} 不在线")


def get_cached_ban_location(nucleus_id: int) -> dict | None:
    return server_cache.get_cached_ban_location(nucleus_id)


async def list_players(
    *,
    status: Literal["online", "offline", "banned", "kicked"] | None = "online",
    name: str | None = None,
    nucleus_id: int | None = None,
    country: str | None = None,
    region: str | None = None,
    page_size: int = 20,
    offset: int = 0,
) -> tuple[list, int]:
    query = Player.all()
    online_nucleus_ids: list[int] = []
    if status in {"online", "offline"}:
        online_nucleus_ids = [int(uid) for uid in server_cache.get_online_nucleus_ids() if uid.isdigit()]
        if status == "online" and not online_nucleus_ids:
            return [], 0
        if status == "online":
            query = query.filter(nucleus_id__in=online_nucleus_ids)
        else:
            query = query.filter(status="offline")
            if online_nucleus_ids:
                query = query.exclude(nucleus_id__in=online_nucleus_ids)
    elif status:
        query = query.filter(status=status)
    if name:
        query = query.filter(name__icontains=name)
    if nucleus_id:
        query = query.filter(nucleus_id=nucleus_id)
    if country:
        query = query.filter(country__icontains=country)
    if region:
        query = query.filter(region__icontains=region)

    total = await query.count()
    players = await query.limit(page_size).offset(offset).values()
    if status == "online":
        for player in players:
            nucleus_id = player.get("nucleus_id")
            loc = server_cache.get_online_location(nucleus_id) if nucleus_id else None
            if not loc:
                continue
            player["status"] = "online"
            player["online_at"] = loc.get("online_at")
            player["ip"] = loc.get("player_ip") or player.get("ip")
            player["country"] = loc.get("player_country") or player.get("country")
            player["region"] = loc.get("player_region") or player.get("region")
            player["input_device"] = loc.get("input_device") or player.get("input_device") or "unknown"
    return players, total


async def query_players(q: str, *, page_size: int = 20, offset: int = 0) -> list[dict]:
    query_text = q.strip()
    filter_q = Q(Q(nucleus_hash__iexact=query_text) | Q(name__icontains=query_text))
    if query_text.isdigit():
        filter_q |= Q(nucleus_id=int(query_text))

    players = await Player.filter(filter_q).offset(offset).limit(page_size)
    if not players:
        return []

    results = []
    for player in players:
        target_loc = None
        target_loc_source = "none"

        if player.nucleus_id:
            loc = server_cache.get_online_location(player.nucleus_id)
            if loc:
                target_loc = loc
                target_loc_source = "live"

        if not target_loc and player.status == "banned" and player.nucleus_id:
            cached_loc = server_cache.get_cached_ban_location(player.nucleus_id)
            if cached_loc:
                target_loc = cached_loc
                target_loc_source = "ban_cache"

        is_online = False
        duration = None
        server_info = None
        ping = 0

        if target_loc:
            is_online = target_loc_source == "live"
            server_full_name = target_loc.get("server_name")
            short_name = target_loc.get("short_name")
            server_host = target_loc.get("server_host")
            server_port = target_loc.get("server_port")
            if not server_full_name or _looks_like_server_address(server_full_name) or _looks_like_server_address(short_name):
                resolved_full_name, resolved_short_name = await _resolve_server_display_by_address(server_host, server_port)
                server_full_name = resolved_full_name or server_full_name
                short_name = resolved_short_name or short_name
            if not short_name:
                short_name = parse_short_name(server_full_name or "")

            server_info = {
                "name": server_full_name,
                "short_name": short_name,
                "host": server_host,
                "port": server_port,
                "ip": server_host,
                "country": target_loc.get("country"),
                "region": target_loc.get("region"),
                "ping": target_loc.get("server_ping"),
            }

            if is_online:
                ping = target_loc.get("ping", 0)
                online_at = target_loc.get("online_at")
                if online_at:
                    duration = (datetime.now(CN_TZ) - online_at).total_seconds()

        if not is_online and player.status == "online":
            is_online = True
            ping = player.ping
            if player.online_at:
                duration = (datetime.now(CN_TZ) - player.online_at).total_seconds()

        player_country = player.country
        player_region = player.region
        if target_loc:
            player_country = target_loc.get("player_country") or player_country
            player_region = target_loc.get("player_region") or player_region
        player_input_device = (target_loc or {}).get("input_device") or player.input_device or "unknown"

        total_playtime = player.total_playtime_seconds
        if is_online and duration is not None:
            total_playtime += int(duration)

        access = await player_access_service.get_player_access_state(player=player)

        results.append({
            "is_online": is_online,
            "server": server_info,
            "server_source": target_loc_source,
            "duration_seconds": int(duration) if duration is not None else 0,
            "total_playtime_seconds": total_playtime,
            "ping": ping,
            "access": access,
            "player": {
                "name": player.name,
                "nucleus_id": player.nucleus_id,
                "status": player.status,
                "ban_count": player.ban_count,
                "kick_count": player.kick_count,
                "country": player_country,
                "region": player_region,
                "input_device": player_input_device,
            },
        })

    return results
