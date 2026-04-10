from fastapi import APIRouter, Depends
from pydantic import BaseModel
from shared_lib.models import UserBinding

from fastapi_service.core.auth import verify_app_key, verify_token
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, success
from fastapi_service.services import binding_service

router = APIRouter(tags=["user"])


class BindRequest(BaseModel):
    platform: str  # "qq" / "kaiheila"
    platform_uid: str
    player_query: str  # 游戏昵称或 nucleus_id


class AdminBindRequest(BaseModel):
    platform: str
    platform_uid: str  # 目标用户的平台ID (如QQ号)
    player_query: str  # 游戏昵称或 nucleus_id


def _bind_error_response(err: str):
    if "未找到" in err:
        return error(ErrorCode.BINDING_PLAYER_NOT_FOUND, msg=err)
    if "已绑定" in err:
        return error(ErrorCode.BINDING_ALREADY_EXISTS, msg=err)
    if "多个" in err:
        return error(ErrorCode.BINDING_PLAYER_AMBIGUOUS, msg=err)
    return error(ErrorCode.BINDING_NOT_FOUND, msg=err)


@router.post("/user/bind", dependencies=[Depends(verify_token)])
async def bind_player(payload: BindRequest):
    """绑定平台账号到游戏玩家（由 NoneBot 调用，用户自行绑定）。"""
    data, err = await binding_service.bind_player(
        platform=payload.platform,
        platform_uid=payload.platform_uid,
        player_query=payload.player_query,
    )
    if err:
        return _bind_error_response(err)
    return success(data=data, msg="绑定成功")


@router.post("/user/admin/bind", dependencies=[Depends(verify_token)])
async def admin_bind_player(payload: AdminBindRequest):
    """管理员绑定指定QQ到指定游戏玩家。"""
    data, err = await binding_service.bind_player(
        platform=payload.platform,
        platform_uid=payload.platform_uid,
        player_query=payload.player_query,
    )
    if err:
        return _bind_error_response(err)
    return success(data=data, msg="管理员绑定成功")


@router.delete("/user/bind", dependencies=[Depends(verify_token)])
async def unbind_player(platform: str, platform_uid: str):
    """解除绑定（用户自行解绑或管理员解绑指定用户）。"""
    deleted = await binding_service.unbind(platform, platform_uid)
    if not deleted:
        return error(ErrorCode.BINDING_NOT_FOUND, msg="未找到绑定记录")
    return success(msg="解绑成功")


@router.get("/user/bind", dependencies=[Depends(verify_token)])
async def get_binding(platform: str, platform_uid: str):
    """查询绑定信息（由 NoneBot 调用）。"""
    data = await binding_service.get_binding(platform, platform_uid)
    if not data:
        return error(ErrorCode.BINDING_NOT_FOUND, msg="未绑定")
    return success(data=data)


@router.get("/user/me")
async def get_me(binding: UserBinding = Depends(verify_app_key)):
    """前端通过 AppKey 获取个人信息。"""
    data = await binding_service.get_binding_by_app_key(binding.app_key)
    if not data:
        return error(ErrorCode.BINDING_NOT_FOUND, msg="AppKey 无效")
    return success(data=data)
