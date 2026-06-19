from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from shared_lib.config import settings

from fastapi_service.core.auth import security_scheme
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, paginated, success
from fastapi_service.services import match_service, player_service
from fastapi_service.services.server_resolver import resolve_server

from ..deps import Pagination, get_pagination

router = APIRouter()


class MatchMetrics(BaseModel):
    model_config = ConfigDict(extra="allow")

    kills: int = 0
    damage: int = 0
    damageTaken: int = 0
    shotsFired: int = 0
    shotsHit: int = 0
    accuracy: float = 0
    accuracyPercent: float = 0
    matches: int = 0
    wins: int = 0
    rankedScore: int = 0
    characterName: str = ""


class WeaponKill(BaseModel):
    model_config = ConfigDict(extra="allow")

    weapon: str
    kills: int


class MatchReportPlayer(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True, extra="allow")

    uid: str
    nucleusId: int
    playerName: str
    team: int = 0
    lifeState: int = 0
    eliminated: bool = False
    metrics: MatchMetrics = Field(default_factory=MatchMetrics)
    tracker: dict[str, Any] = Field(default_factory=dict)
    weaponKills: list[WeaponKill] = Field(default_factory=list)


class MatchEndReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    serverId: str
    serverIp: str
    serverPort: int
    map: str
    playlist: str
    sdkVersion: str
    tick: int
    spawnCount: int
    endedAt: int
    numPlayers: int
    maxPlayers: int
    players: list[MatchReportPlayer] = Field(default_factory=list)


def _verify_optional_sdk_token(credentials: HTTPAuthorizationCredentials | None) -> None:
    if not credentials:
        return
    if settings.fastapi_access_tokens and credentials.credentials not in settings.fastapi_access_tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证令牌无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _server_info(server) -> dict:
    return {
        "id": server.id,
        "host": server.host,
        "name": server.name,
        "short_name": server.short_name,
    }


@router.post("/matches/end")
async def report_match_end(
    payload: MatchEndReport,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
):
    """接收 SDK 在 GameShutdown 后发送的对局结算报告。"""
    _verify_optional_sdk_token(credentials)
    try:
        result = await match_service.process_match_end_report(payload.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return success(data=result, msg="对局结算已接收")


@router.get("/matches/recent")
async def get_recent_matches(
    limit: int = Query(10, ge=1, le=50, description="最多返回的对局数量"),
    min_top_kills: int | None = Query(
        None,
        ge=0,
        description="只包含 top1 击杀数大于等于该值的对局。默认使用 settings.recent_match_top_kills_threshold。",
    ),
    server: str | None = Query(None, description="服务器别名 / IPv4 / 中文名"),
):
    """最近已完成的对局列表（附 top1 玩家）。默认 10 场。"""
    server_obj = None
    if server:
        server_obj = await resolve_server(server)
        if not server_obj:
            return error(ErrorCode.SERVER_NOT_FOUND, f"未找到服务器: {server}")

    threshold = min_top_kills if min_top_kills is not None else settings.recent_match_top_kills_threshold
    results = await match_service.get_recent_matches(
        limit=limit,
        min_top_kills=threshold,
        server_id=server_obj.id if server_obj else None,
    )
    extra: dict = {"min_top_kills": threshold, "limit": limit}
    if server_obj:
        extra["server"] = _server_info(server_obj)
    return success(data=results, msg=f"最近 {len(results)} 场对局", **extra)


@router.get("/matches/player/{nucleus_id_or_player_name}")
async def get_player_matches(
    nucleus_id_or_player_name: int | str,
    limit: int | None = Query(None, ge=1, le=20, description="默认使用 settings.personal_match_default_limit (3)"),
    sort: Literal["time", "kills", "kd"] = "time",
    server: str | None = Query(None, description="服务器别名 / IPv4 / 中文名"),
):
    """玩家最近参与过的 N 场已完成对局（本场 kills/deaths/kd）。"""
    player, err = await player_service.get_player_by_identifier(nucleus_id_or_player_name, require_nucleus_id=False)
    if err:
        return err
    assert player is not None

    server_obj = None
    if server:
        server_obj = await resolve_server(server)
        if not server_obj:
            return error(ErrorCode.SERVER_NOT_FOUND, f"未找到服务器: {server}")

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
    return success(data=results, msg=f"玩家 {player.name} 最近 {len(results)} 场对局", **extra)


@router.get("/leaderboard/competitive")
async def get_competitive_leaderboard(
    range: Literal["today", "week", "last_week"] = "today",
    pg: Pagination = Depends(get_pagination),
    top_per_day: int | None = Query(
        None,
        ge=1,
        le=20,
        description="每名玩家每天最多计入这么多场最高击杀对局。默认使用 settings.competitive_daily_match_limit。",
    ),
    server: str | None = None,
):
    """竞技榜：每人每天取 top N 场击杀，跨天相加后按总击杀降序排。"""
    server_obj = None
    if server:
        server_obj = await resolve_server(server)
        if not server_obj:
            return error(ErrorCode.SERVER_NOT_FOUND, f"未找到服务器: {server}")

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
        msg=f"竞技排行榜 ({range})",
        **extra,
    )
