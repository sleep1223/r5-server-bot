from typing import Literal

from fastapi import APIRouter, Depends, Query

from fastapi_service.core.response import paginated
from fastapi_service.services import leaderboard_service

from ..deps import Pagination, get_pagination

router = APIRouter()


@router.get("/leaderboard/kd")
async def get_kd_leaderboard(
    range: Literal["today", "yesterday", "week", "month"] = "today",
    pg: Pagination = Depends(get_pagination),
    sort: Literal["kills", "deaths", "kd"] = "kd",
    min_kills: int = 100,
    min_deaths: int = 0,
):
    """获取全局 KD 排行榜。"""
    results, total = await leaderboard_service.get_kd_ranking(
        range_type=range,
        sort=sort,
        min_kills=min_kills,
        min_deaths=min_deaths,
        offset=pg.offset,
        page_size=pg.page_size,
    )
    return paginated(data=results, total=total, msg=f"KD Leaderboard for {range} range")


@router.get("/leaderboard/weapon")
async def get_weapon_leaderboard(
    weapon: list[str] = Query(
        default=["r99", "volt", "wingman", "flatline", "r301", "player"],
        description="Weapon names (e.g., r301) or internal codes; multiple allowed",
    ),
    range: Literal["today", "yesterday", "week", "month"] = "today",
    pg: Pagination = Depends(get_pagination),
    sort: Literal["kills", "deaths", "kd"] = "kd",
    min_kills: int = 1,
    min_deaths: int = 0,
):
    """获取武器列表的最佳使用者排行榜（默认按 KD 排序），默认时间范围为今日。"""
    results, total, display_weapons_str = await leaderboard_service.get_weapon_ranking(
        weapons=weapon,
        range_type=range,
        sort=sort,
        min_kills=min_kills,
        min_deaths=min_deaths,
        offset=pg.offset,
        page_size=pg.page_size,
    )
    return paginated(data=results, total=total, msg=f"Weapon Leaderboard for {display_weapons_str} ({range})")
