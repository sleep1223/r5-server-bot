from typing import Literal

from fastapi import APIRouter, Depends

from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, paginated, success
from fastapi_service.services import leaderboard_service, player_service
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


@router.get("/players/{nucleus_id_or_player_name}/vs_all")
async def get_player_vs_all_stats(
    nucleus_id_or_player_name: int | str,
    range: Literal["today", "yesterday", "week", "last_week", "month", "all"] = "all",
    pg: Pagination = Depends(get_pagination),
    sort: Literal["kills", "deaths", "kd"] = "kd",
    server: str | None = None,
):
    """获取特定玩家对其他所有人的 KD（从高到低），默认时间范围为全部。"""
    player, err = await player_service.get_player_by_identifier(nucleus_id_or_player_name, require_nucleus_id=False)
    if err:
        return err
    assert player is not None

    server_obj = None
    if server:
        server_obj = await resolve_server(server)
        if not server_obj:
            return error(ErrorCode.SERVER_NOT_FOUND, f"Server not found: {server}")

    results, total, summary = await leaderboard_service.get_player_vs_all(
        player_id=player.id,
        sort=sort,
        offset=pg.offset,
        page_size=pg.page_size,
        server_id=server_obj.id if server_obj else None,
        range_type=range,
    )

    if not results and total == 0:
        return success(data=[], msg=f"Player {nucleus_id_or_player_name} has no opponents ({range})")

    player_info = {"name": player.name, "nucleus_id": player.nucleus_id, "country": player.country, "region": player.region}
    extra: dict = {"summary": summary, "player": player_info}
    if server_obj:
        extra["server"] = _server_info(server_obj)
    return paginated(data=results, total=total, msg=f"KD Leaderboard for {nucleus_id_or_player_name} ({range})", **extra)


@router.get("/players/{nucleus_id_or_player_name}/weapons")
async def get_player_weapon_stats(
    nucleus_id_or_player_name: int | str,
    range: Literal["today", "yesterday", "week", "last_week", "month", "all"] = "all",
    pg: Pagination = Depends(get_pagination),
    sort: Literal["kills", "deaths", "kd"] = "kd",
    server: str | None = None,
):
    """获取特定玩家的武器统计，默认时间范围为全部。"""
    player, err = await player_service.get_player_by_identifier(nucleus_id_or_player_name, require_nucleus_id=False)
    if err:
        return err
    assert player is not None

    server_obj = None
    if server:
        server_obj = await resolve_server(server)
        if not server_obj:
            return error(ErrorCode.SERVER_NOT_FOUND, f"Server not found: {server}")

    results, total, summary = await leaderboard_service.get_player_weapon_stats(
        player_id=player.id,
        sort=sort,
        offset=pg.offset,
        page_size=pg.page_size,
        server_id=server_obj.id if server_obj else None,
        range_type=range,
    )

    if not results and total == 0:
        return success(data=[], msg=f"Player {nucleus_id_or_player_name} has no weapon stats ({range})")

    player_info = {"name": player.name, "nucleus_id": player.nucleus_id, "country": player.country, "region": player.region}
    extra: dict = {"summary": summary, "player": player_info}
    if server_obj:
        extra["server"] = _server_info(server_obj)
    return paginated(data=results, total=total, msg=f"Weapon stats for {nucleus_id_or_player_name} ({range})", **extra)
