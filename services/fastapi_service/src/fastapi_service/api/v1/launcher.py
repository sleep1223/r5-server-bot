from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from fastapi_service.core.response import success
from fastapi_service.services import launcher_service

router = APIRouter()


@router.get("/launcher/config")
async def get_launcher_config():
    """获取 R5RCN Launcher 配置信息（读取 TOML 文件并返回）"""
    try:
        data = launcher_service.get_launcher_config()
    except launcher_service.LauncherConfigError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return success(data=data, msg="Launcher config retrieved")


@router.get("/launcher/update/{target}/{arch}/{current_version}")
async def check_launcher_update(target: str, arch: str, current_version: str):
    """Tauri 自更新检查接口

    - 有更新时返回 HTTP 200 + Tauri updater JSON
    - 无更新时返回 HTTP 204
    """
    try:
        payload = launcher_service.get_launcher_update(target, arch, current_version)
    except launcher_service.LauncherConfigError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    if payload is None:
        return Response(status_code=204)

    # 返回 Tauri updater 格式
    return JSONResponse(content=payload)
