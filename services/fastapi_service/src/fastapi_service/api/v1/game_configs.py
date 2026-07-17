from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from shared_lib.config import settings
from shared_lib.models import UserBinding

from fastapi_service.core.auth import verify_admin_app_key, verify_app_key
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, paginated, success
from fastapi_service.services import game_config_service

from ..deps import Pagination, get_pagination

router = APIRouter(prefix="/launcher/game-configs", tags=["game-configs"])


class SaveGameConfigRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    remark: str | None = Field(default=None, max_length=500)
    source_game: str = Field(min_length=1, max_length=8)
    content: str = Field(min_length=1)


@router.get("")
async def list_game_configs(
    pg: Pagination = Depends(get_pagination),
    q: str | None = Query(default=None, max_length=100),
    input_device: Literal["mouse_keyboard", "controller"] | None = None,
):
    items, total = await game_config_service.list_presets(
        page_size=pg.page_size,
        offset=pg.offset,
        q=q,
        input_device=input_device,
    )
    return paginated(data=items, total=total, page_no=pg.page_no, page_size=pg.page_size)


# Static /mine routes must stay above /{preset_id}.
@router.get("/mine")
async def get_my_game_config(binding: UserBinding = Depends(verify_app_key)):
    data = await game_config_service.get_mine(binding.id)
    if data is None:
        return error(ErrorCode.GAME_CONFIG_NOT_FOUND, msg="尚未上传配置")
    return success(data=data)


@router.put("/mine")
async def save_my_game_config(
    payload: SaveGameConfigRequest,
    binding: UserBinding = Depends(verify_app_key),
):
    if not settings.game_config_upload_enabled:
        return error(ErrorCode.GAME_CONFIG_UPLOAD_DISABLED, msg="配置上传功能暂未开放")
    try:
        data = await game_config_service.save_mine(
            binding,
            name=payload.name,
            remark=payload.remark,
            source_game=payload.source_game,
            content=payload.content,
        )
    except game_config_service.GameConfigValidationError as exc:
        if "来源游戏无效" in str(exc):
            code = ErrorCode.GAME_CONFIG_INVALID_SOURCE_GAME
        elif "超过大小限制" in str(exc):
            code = ErrorCode.GAME_CONFIG_CONTENT_TOO_LARGE
        else:
            code = ErrorCode.GAME_CONFIG_INVALID_CONTENT
        return error(code, msg=str(exc))
    return success(data=data, msg="配置已保存")


@router.delete("/mine")
async def delete_my_game_config(binding: UserBinding = Depends(verify_app_key)):
    if not await game_config_service.delete_mine(binding.id):
        return error(ErrorCode.GAME_CONFIG_NOT_FOUND, msg="配置不存在")
    return success(msg="配置已删除")


@router.get("/{preset_id}")
async def get_game_config(preset_id: int):
    data = await game_config_service.get_preset(preset_id)
    if data is None:
        return error(ErrorCode.GAME_CONFIG_NOT_FOUND, msg="配置不存在")
    return success(data=data)


@router.delete("/{preset_id}")
async def admin_delete_game_config(
    preset_id: int,
    _binding: UserBinding = Depends(verify_admin_app_key),
):
    if not await game_config_service.delete_preset(preset_id):
        return error(ErrorCode.GAME_CONFIG_NOT_FOUND, msg="配置不存在")
    return success(msg="配置已由管理员删除")
