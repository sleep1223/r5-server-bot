from typing import Literal

from fastapi import APIRouter, Depends, Query
from shared_lib.config import settings

from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, paginated, success
from fastapi_service.services import match_service, player_service
from fastapi_service.services.server_resolver import resolve_server

from ..deps import Pagination, get_pagination

router = APIRouter()


def _server_info(server) -> dict:
    return {
        "id": server.id,
        "host": server.host,
        "name": server.name,
        "short_name": server.short_name,
    }


@router.get("/matches/recent")
async def get_recent_matches(
    limit: int = Query(10, ge=1, le=50, description="Max matches to return"),
    min_top_kills: int | None = Query(
        None,
        ge=0,
        description="Only include matches where top1's kills >= this value. Defaults to settings.recent_match_top_kills_threshold.",
    ),
    server: str | None = Query(None, description="Server alias / IPv4 / Chinese name"),
):
    """最近已完成的对局列表（附 top1 玩家）。默认 10 场。"""
    server_obj = None
    if server:
        server_obj = await resolve_server(server)
        if not server_obj:
            return error(ErrorCode.SERVER_NOT_FOUND, f"Server not found: {server}")

    threshold = min_top_kills if min_top_kills is not None else settings.recent_match_top_kills_threshold
    results = await match_service.get_recent_matches(
        limit=limit,
        min_top_kills=threshold,
        server_id=server_obj.id if server_obj else None,
    )
    extra: dict = {"min_top_kills": threshold, "limit": limit}
    if server_obj:
        extra["server"] = _server_info(server_obj)
    return success(data=results, msg=f"Recent {len(results)} matches", **extra)


@router.get("/matches/player/{nucleus_id_or_player_name}")
async def get_player_matches(
    nucleus_id_or_player_name: int | str,
    limit: int | None = Query(
        None, ge=1, le=20, description="Default: settings.personal_match_default_limit (3)"
    ),
    sort: Literal["time", "kills", "kd"] = "time",
    server: str | None = Query(None, description="Server alias / IPv4 / Chinese name"),
):
    """玩家最近参与过的 N 场已完成对局（本场 kills/deaths/kd）。"""
    player, err = await player_service.get_player_by_identifier(
        nucleus_id_or_player_name, require_nucleus_id=False
    )
    if err:
        return err
    assert player is not None

    server_obj = None
    if server:
        server_obj = await resolve_server(server)
        if not server_obj:
            return error(ErrorCode.SERVER_NOT_FOUND, f"Server not found: {server}")

    effective_limit = limit if limit is not None else settings.personal_match_default_limit
    results = await match_service.get_player_matches(
        player_id=player.id,
        limit=effective_limit,
        sort=sort,
        server_id=server_obj.id if server_obj else None,
    )
    player_info = {
        "name": player.name,
        "nucleus_id": player.nucleus_id,
        "country": player.country,
        "region": player.region,
    }
    extra: dict = {"player": player_info, "limit": effective_limit, "sort": sort}
    if server_obj:
        extra["server"] = _server_info(server_obj)
    return success(data=results, msg=f"Recent {len(results)} matches for {player.name}", **extra)


@router.get("/leaderboard/competitive")
async def get_competitive_leaderboard(
    range: Literal["today", "week", "last_week"] = "today",
    pg: Pagination = Depends(get_pagination),
    top_per_day: int | None = Query(
        None,
        ge=1,
        le=20,
        description="Each player counts at most this many best-kill matches per day. "
        "Defaults to settings.competitive_daily_match_limit.",
    ),
    server: str | None = None,
):
    """竞技榜：每人每天取 top N 场击杀，跨天相加后按总击杀降序排。"""
    server_obj = None
    if server:
        server_obj = await resolve_server(server)
        if not server_obj:
            return error(ErrorCode.SERVER_NOT_FOUND, f"Server not found: {server}")

    effective_top = top_per_day if top_per_day is not None else settings.competitive_daily_match_limit
    results, total = await match_service.get_competitive_ranking(
        range_type=range,
        limit=pg.page_size,
        offset=pg.offset,
        top_per_day=effective_top,
        server_id=server_obj.id if server_obj else None,
    )
    extra: dict = {"top_per_day": effective_top, "range": range}
    if server_obj:
        extra["server"] = _server_info(server_obj)
    return paginated(
        data=results,
        total=total,
        msg=f"Competitive Leaderboard ({range})",
        **extra,
    )
