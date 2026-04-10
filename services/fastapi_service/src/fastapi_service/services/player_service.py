from __future__ import annotations

from datetime import datetime
from typing import Literal

from shared_lib.models import Player
from tortoise.expressions import Q

from fastapi_service.core.cache import server_cache
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error
from fastapi_service.core.utils import CN_TZ, parse_short_name


async def get_player_by_identifier(identifier: int | str, require_nucleus_id: bool = True) -> tuple[Player, None] | tuple[None, dict]:
    identifier_text = str(identifier).strip()
    filter_q = Q(Q(nucleus_hash__iexact=identifier_text) | Q(name__iexact=identifier_text))
    if identifier_text.isdigit():
        filter_q |= Q(nucleus_id=int(identifier_text))

    player = await Player.filter(filter_q).first()
    if not player:
        return None, error(ErrorCode.PLAYER_NOT_FOUND, msg=f"Player {identifier} not found")

    if require_nucleus_id and not player.nucleus_id:
        return None, error(ErrorCode.PLAYER_NO_NUCLEUS_ID, msg=f"Player {identifier} has no nucleus_id")

    return player, None


def get_online_location(player: Player) -> tuple[dict | None, dict | None]:
    if not player.nucleus_id:
        return None, error(ErrorCode.PLAYER_NOT_ONLINE, msg=f"Player {player.name} is not online")

    loc = server_cache.get_online_location(player.nucleus_id)
    if loc:
        return loc, None
    return None, error(ErrorCode.PLAYER_NOT_ONLINE, msg=f"Player {player.name} is not online")


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
    if status:
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
            if not short_name:
                short_name = parse_short_name(server_full_name or "")

            server_info = {
                "name": server_full_name,
                "short_name": short_name,
                "host": target_loc.get("server_host"),
                "port": target_loc.get("server_port"),
                "ip": target_loc.get("server_host"),
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

        results.append({
            "is_online": is_online,
            "server": server_info,
            "server_source": target_loc_source,
            "duration_seconds": int(duration) if duration is not None else 0,
            "ping": ping,
            "player": {
                "name": player.name,
                "nucleus_id": player.nucleus_id,
                "status": player.status,
                "ban_count": player.ban_count,
                "kick_count": player.kick_count,
                "country": player.country,
                "region": player.region,
            },
        })

    return results
