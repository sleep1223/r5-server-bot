from typing import Literal

from fastapi import APIRouter, Depends, Query

from fastapi_service.core.auth import verify_token
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, paginated, success
from fastapi_service.services import player_service

from ..deps import Pagination, get_pagination

router = APIRouter()


@router.get("/players", dependencies=[Depends(verify_token)])
async def get_players(
    status: Literal["online", "offline", "banned", "kicked"] | None = "online",
    name: str | None = None,
    nucleus_id: int | None = None,
    country: str | None = None,
    region: str | None = None,
    pg: Pagination = Depends(get_pagination),
):
    items, total = await player_service.list_players(
        status=status,
        name=name,
        nucleus_id=nucleus_id,
        country=country,
        region=region,
        page_size=pg.page_size,
        offset=pg.offset,
    )
    return paginated(data=items, total=total, msg="Players retrieved")


@router.get("/players/query")
async def query_player(
    q: int | str,
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=20, description="Items per page"),
):
    """通过 nucleus_id (int/str) 或名称 (模糊搜索) 查询玩家。"""
    offset = (page_no - 1) * page_size
    results = await player_service.query_players(str(q), page_size=page_size, offset=offset)
    if not results:
        return error(ErrorCode.PLAYER_NOT_FOUND, msg=f"No players found matching '{q}'", data=[])
    return success(data=results, msg=f"Found {len(results)} players")
