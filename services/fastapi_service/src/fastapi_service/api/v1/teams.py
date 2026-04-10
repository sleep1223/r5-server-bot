from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from shared_lib.models import UserBinding

from fastapi_service.core.auth import verify_app_key, verify_token
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, paginated, success
from fastapi_service.services import team_service

from ..deps import Pagination, get_pagination

router = APIRouter(tags=["teams"])


# ── Request Models ──────────────────────────────────────────────


class CreateTeamRequest(BaseModel):
    platform: str
    platform_uid: str
    slots_needed: int = Field(..., ge=1, le=2)


class JoinTeamRequest(BaseModel):
    platform: str
    platform_uid: str


class InviteRequest(BaseModel):
    platform: str
    platform_uid: str
    target_player_name: str


class AcceptInviteRequest(BaseModel):
    platform: str
    platform_uid: str


# ── Bot 端接口 (Bearer Token 认证) ──────────────────────────────


@router.post("/teams", dependencies=[Depends(verify_token)])
async def create_team(payload: CreateTeamRequest):
    """创建组队（由 NoneBot 调用）。"""
    binding = await team_service.get_binding_by_platform(payload.platform, payload.platform_uid)
    if not binding:
        return error(ErrorCode.BINDING_NOT_FOUND, msg="请先绑定游戏账号")

    data, err = await team_service.create_team(binding.id, payload.slots_needed)
    if err:
        return error(ErrorCode.TEAM_ALREADY_IN_TEAM, msg=err)
    return success(data=data, msg="组队创建成功")


@router.get("/teams")
async def list_teams(pg: Pagination = Depends(get_pagination)):
    """列出所有开放的队伍。"""
    items, total = await team_service.list_open_teams(page_size=pg.page_size, offset=pg.offset)
    return paginated(data=items, total=total, msg="Teams retrieved")


@router.get("/teams/{team_id}")
async def get_team(team_id: int):
    """获取队伍详情。"""
    data = await team_service.get_team_detail(team_id)
    if not data:
        return error(ErrorCode.TEAM_NOT_FOUND, msg="队伍不存在")
    return success(data=data)


@router.post("/teams/{team_id}/join", dependencies=[Depends(verify_token)])
async def join_team(team_id: int, payload: JoinTeamRequest):
    """加入队伍（由 NoneBot 调用）。"""
    binding = await team_service.get_binding_by_platform(payload.platform, payload.platform_uid)
    if not binding:
        return error(ErrorCode.BINDING_NOT_FOUND, msg="请先绑定游戏账号")

    data, err = await team_service.join_team(team_id, binding.id)
    if err:
        if "已满" in err:
            return error(ErrorCode.TEAM_ALREADY_FULL, msg=err)
        if "已有" in err:
            return error(ErrorCode.TEAM_ALREADY_IN_TEAM, msg=err)
        return error(ErrorCode.TEAM_NOT_FOUND, msg=err)

    # 检查是否满员，返回成员信息用于通知
    notify_members = None
    if data and data.get("status") == "full":
        notify_members = await team_service.get_full_team_members(team_id)

    return success(data={"team": data, "notify_members": notify_members}, msg="加入成功")


@router.post("/teams/{team_id}/cancel", dependencies=[Depends(verify_token)])
async def cancel_team(team_id: int, payload: JoinTeamRequest):
    """取消组队（仅队长，由 NoneBot 调用）。"""
    binding = await team_service.get_binding_by_platform(payload.platform, payload.platform_uid)
    if not binding:
        return error(ErrorCode.BINDING_NOT_FOUND, msg="请先绑定游戏账号")

    ok, err = await team_service.cancel_team(team_id, binding.id)
    if not ok:
        return error(ErrorCode.TEAM_NOT_CREATOR, msg=err)
    return success(msg="组队已取消")


@router.post("/teams/{team_id}/leave", dependencies=[Depends(verify_token)])
async def leave_team(team_id: int, payload: JoinTeamRequest):
    """退出队伍（非队长，由 NoneBot 调用）。"""
    binding = await team_service.get_binding_by_platform(payload.platform, payload.platform_uid)
    if not binding:
        return error(ErrorCode.BINDING_NOT_FOUND, msg="请先绑定游戏账号")

    ok, err = await team_service.leave_team(team_id, binding.id)
    if not ok:
        return error(ErrorCode.TEAM_NOT_MEMBER, msg=err)
    return success(msg="已退出队伍")


@router.post("/teams/{team_id}/invite", dependencies=[Depends(verify_token)])
async def invite_player(team_id: int, payload: InviteRequest):
    """邀请玩家（仅队长，由 NoneBot 调用）。"""
    binding = await team_service.get_binding_by_platform(payload.platform, payload.platform_uid)
    if not binding:
        return error(ErrorCode.BINDING_NOT_FOUND, msg="请先绑定游戏账号")

    data, err = await team_service.invite_player(team_id, binding.id, payload.target_player_name)
    if err:
        return error(ErrorCode.TEAM_NOT_FOUND, msg=err)
    return success(data=data, msg="邀请已发送")


@router.post("/teams/{team_id}/accept", dependencies=[Depends(verify_token)])
async def accept_invite(team_id: int, payload: AcceptInviteRequest):
    """接受邀请加入队伍（由 NoneBot 调用）。"""
    binding = await team_service.get_binding_by_platform(payload.platform, payload.platform_uid)
    if not binding:
        return error(ErrorCode.BINDING_NOT_FOUND, msg="请先绑定游戏账号")

    data, err = await team_service.accept_invite(team_id, binding.id)
    if err:
        if "已满" in err:
            return error(ErrorCode.TEAM_ALREADY_FULL, msg=err)
        if "已有" in err:
            return error(ErrorCode.TEAM_ALREADY_IN_TEAM, msg=err)
        return error(ErrorCode.TEAM_NOT_FOUND, msg=err)

    notify_members = None
    if data and data.get("status") == "full":
        notify_members = await team_service.get_full_team_members(team_id)

    return success(data={"team": data, "notify_members": notify_members}, msg="已加入队伍")


# ── 前端接口 (AppKey 认证) ──────────────────────────────────────


@router.post("/teams/app/create")
async def app_create_team(slots_needed: int = Field(..., ge=1, le=2), binding: UserBinding = Depends(verify_app_key)):
    """前端创建组队。"""
    data, err = await team_service.create_team(binding.id, slots_needed)
    if err:
        return error(ErrorCode.TEAM_ALREADY_IN_TEAM, msg=err)
    return success(data=data, msg="组队创建成功")


@router.post("/teams/app/{team_id}/join")
async def app_join_team(team_id: int, binding: UserBinding = Depends(verify_app_key)):
    """前端加入队伍。"""
    data, err = await team_service.join_team(team_id, binding.id)
    if err:
        if "已满" in err:
            return error(ErrorCode.TEAM_ALREADY_FULL, msg=err)
        if "已有" in err:
            return error(ErrorCode.TEAM_ALREADY_IN_TEAM, msg=err)
        return error(ErrorCode.TEAM_NOT_FOUND, msg=err)

    notify_members = None
    if data and data.get("status") == "full":
        notify_members = await team_service.get_full_team_members(team_id)

    return success(data={"team": data, "notify_members": notify_members}, msg="加入成功")


@router.post("/teams/app/{team_id}/cancel")
async def app_cancel_team(team_id: int, binding: UserBinding = Depends(verify_app_key)):
    """前端取消组队。"""
    ok, err = await team_service.cancel_team(team_id, binding.id)
    if not ok:
        return error(ErrorCode.TEAM_NOT_CREATOR, msg=err)
    return success(msg="组队已取消")


@router.post("/teams/app/{team_id}/leave")
async def app_leave_team(team_id: int, binding: UserBinding = Depends(verify_app_key)):
    """前端退出队伍。"""
    ok, err = await team_service.leave_team(team_id, binding.id)
    if not ok:
        return error(ErrorCode.TEAM_NOT_MEMBER, msg=err)
    return success(msg="已退出队伍")
