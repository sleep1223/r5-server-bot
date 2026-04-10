from typing import Literal

from fastapi import APIRouter, Depends

from fastapi_service.core.response import paginated, success
from fastapi_service.services import leaderboard_service, player_service

from ..deps import Pagination, get_pagination

router = APIRouter()


@router.get("/players/{nucleus_id_or_player_name}/vs_all")
async def get_player_vs_all_stats(
    nucleus_id_or_player_name: int | str,
    pg: Pagination = Depends(get_pagination),
    sort: Literal["kills", "deaths", "kd"] = "kd",
):
    """获取特定玩家对其他所有人的 KD（从高到低）。"""
    player, err = await player_service.get_player_by_identifier(nucleus_id_or_player_name, require_nucleus_id=False)
    if err:
        return err
    assert player is not None

    results, total, summary = await leaderboard_service.get_player_vs_all(
        player_id=player.id,
        sort=sort,
        offset=pg.offset,
        page_size=pg.page_size,
    )

    if not results and total == 0:
        return success(data=[], msg=f"Player {nucleus_id_or_player_name} has no opponents")

    player_info = {"name": player.name, "nucleus_id": player.nucleus_id, "country": player.country, "region": player.region}
    return paginated(data=results, total=total, msg=f"KD Leaderboard for {nucleus_id_or_player_name}", summary=summary, player=player_info)


@router.get("/players/{nucleus_id_or_player_name}/weapons")
async def get_player_weapon_stats(
    nucleus_id_or_player_name: int | str,
    pg: Pagination = Depends(get_pagination),
    sort: Literal["kills", "deaths", "kd"] = "kd",
):
    """获取特定玩家的武器统计。"""
    player, err = await player_service.get_player_by_identifier(nucleus_id_or_player_name, require_nucleus_id=False)
    if err:
        return err
    assert player is not None

    results, total, summary = await leaderboard_service.get_player_weapon_stats(
        player_id=player.id,
        sort=sort,
        offset=pg.offset,
        page_size=pg.page_size,
    )

    if not results and total == 0:
        return success(data=[], msg=f"Player {nucleus_id_or_player_name} has no weapon stats")

    player_info = {"name": player.name, "nucleus_id": player.nucleus_id, "country": player.country, "region": player.region}
    return paginated(data=results, total=total, msg=f"Weapon stats for {nucleus_id_or_player_name}", summary=summary, player=player_info)
