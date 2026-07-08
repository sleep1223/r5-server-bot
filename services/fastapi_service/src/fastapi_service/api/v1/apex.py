from typing import Literal

from fastapi import APIRouter, Query

from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, success
from fastapi_service.services import apex_service
from fastapi_service.services.apex_service import ApexServiceError
from fastapi_service.services.apex_translations import apex_translations_payload

router = APIRouter()


def _apex_error(exc: ApexServiceError) -> dict:
    code = ErrorCode.APEX_INVALID_REQUEST if exc.status_code == 400 else ErrorCode.APEX_API_ERROR
    if exc.status_code == 500:
        code = ErrorCode.APEX_NOT_CONFIGURED
    return error(code, exc.message, data={"api_status_code": exc.api_status_code})


@router.get("/apex/player")
async def get_apex_player(
    player_name: str | None = Query(default=None, description="EA/Origin 玩家名"),
    uid: str | None = Query(default=None, description="Apex/EA UID"),
    platform: Literal["PC", "PS4", "X1", "SWITCH"] = "PC",
    resolve_uid_first: bool = Query(default=False, description="传 player_name 时先 nametouid，再用 UID 查询 bridge"),
    save_snapshot: bool = Query(default=True, description="是否保存快照并返回与上次查询的差异"),
):
    """查询玩家数据，并自动保存历史快照用于变化趋势对比。"""
    try:
        data = await apex_service.get_player_stats(
            player_name=player_name,
            uid=uid,
            platform=platform,
            resolve_uid_first=resolve_uid_first,
            save_snapshot=save_snapshot,
        )
    except ApexServiceError as exc:
        return _apex_error(exc)
    return success(data=data, msg="Apex 玩家数据已获取")


@router.get("/apex/player/uid")
async def get_apex_player_uid(
    player_name: str = Query(..., description="EA/Origin 玩家名"),
    platform: Literal["PC", "PS4", "X1", "SWITCH"] = "PC",
):
    """根据用户名查询 UID。"""
    try:
        data = await apex_service.resolve_uid(player_name, platform)
    except ApexServiceError as exc:
        return _apex_error(exc)
    return success(data=data, msg="Apex 玩家 UID 已获取")


@router.get("/apex/player/history")
async def get_apex_player_history(
    player_name: str | None = Query(default=None, description="EA/Origin 玩家名"),
    uid: str | None = Query(default=None, description="Apex/EA UID"),
    platform: Literal["PC", "PS4", "X1", "SWITCH"] = "PC",
    limit: int = Query(default=20, ge=1, le=100, description="返回最近 N 条快照"),
    resolve_uid_first: bool = Query(default=False, description="传 player_name 时先 nametouid，再用 UID 查历史"),
):
    """查询玩家历史快照和相邻快照变化。"""
    try:
        data = await apex_service.get_player_history(
            player_name=player_name,
            uid=uid,
            platform=platform,
            limit=limit,
            resolve_uid_first=resolve_uid_first,
        )
    except ApexServiceError as exc:
        return _apex_error(exc)
    return success(data=data, msg="Apex 玩家历史已获取")


@router.get("/apex/translations")
async def get_apex_translations():
    """获取 Apex 英文文案到其它语言的翻译表。"""
    return success(data=apex_translations_payload(), msg="Apex 翻译表已获取")


@router.get("/apex/map-rotation")
async def get_apex_map_rotation():
    """查询地图轮换（大逃杀 / 排位赛 / 混录带）。"""
    try:
        data = await apex_service.get_cached_resource("map_rotation")
    except ApexServiceError as exc:
        return _apex_error(exc)
    return success(data=data, msg="Apex 地图轮换已获取")


@router.get("/apex/server-status")
async def get_apex_server_status():
    """查询 Apex 各分区官方服务器运行状态。"""
    try:
        data = await apex_service.get_cached_resource("server_status")
    except ApexServiceError as exc:
        return _apex_error(exc)
    return success(data=data, msg="Apex 服务器状态已获取")


@router.get("/apex/predator")
async def get_apex_predator():
    """查询各平台顶尖猎杀者分数线。"""
    try:
        data = await apex_service.get_cached_resource("predator")
    except ApexServiceError as exc:
        return _apex_error(exc)
    return success(data=data, msg="Apex 顶猎分数已获取")
