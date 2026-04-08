import tomllib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from loguru import logger
from shared_lib.config import settings

from ..response import success

router = APIRouter()


@router.get("/launcher/config")
async def get_launcher_config():
    """获取 R5RC N Launcher 配置信息（读取 TOML 文件并返回）"""
    config_path = Path(settings.launcher_config_path)
    if not config_path.exists():
        logger.error(f"Launcher config file not found: {config_path}")
        raise HTTPException(status_code=404, detail="Launcher config not found")

    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        logger.error(f"Failed to parse launcher config: {e}")
        raise HTTPException(status_code=500, detail="Invalid launcher config") from e

    return success(data=data, msg="Launcher config retrieved")
