import copy
import tomllib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from loguru import logger
from shared_lib.config import settings

from fastapi_service.core.response import success

router = APIRouter()


def _load_toml(path: Path) -> dict:
    """读取并解析 TOML 文件"""
    if not path.exists():
        logger.error(f"Config file not found: {path}")
        raise HTTPException(status_code=404, detail=f"Config file not found: {path.name}")
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        logger.error(f"Failed to parse config: {e}")
        raise HTTPException(status_code=500, detail="Invalid config file") from e


def _parse_version(v: str) -> tuple[int, ...]:
    """将版本号字符串解析为整数元组，用于比较。如 '0.4.0' -> (0, 4, 0)"""
    return tuple(int(x) for x in v.strip().lstrip("v").split("."))


def _resolve_latest(data: dict) -> tuple[str, dict | None]:
    """解析最新版本号和对应的版本信息，如果 latest 在 versions 中找不到则 fallback 到第一个条目并替换版本号"""
    latest_version = data.get("latest", "")
    versions: list[dict] = data.get("versions", [])
    if not latest_version and not versions:
        return "", None
    version_info = next((v for v in versions if v.get("version") == latest_version), None)
    if not version_info and versions:
        fallback = versions[0]
        fallback_version = fallback.get("version", "")
        logger.warning(
            f"Configured latest version '{latest_version}' not found in versions list, "
            f"falling back from '{fallback_version}'"
        )
        # 只替换顶层 version 字段；URL/签名等可能含版本号的字段保持原样，
        # 避免破坏 fallback 条目里的下载地址或哈希。
        version_info = copy.deepcopy(fallback)
        version_info["version"] = latest_version
    return latest_version, version_info


@router.get("/launcher/config")
async def get_launcher_config():
    """获取 R5RCN Launcher 配置信息（读取 TOML 文件并返回）"""
    data = _load_toml(Path(settings.launcher_config_path))
    update_data = _load_toml(Path(settings.launcher_update_path))
    # 从最新版本的平台信息中提取下载地址，默认取 windows-x86_64
    latest_version, version_info = _resolve_latest(update_data)
    data["launcher_version"] = latest_version
    if version_info:
        platform_info = version_info.get("platforms", {}).get("windows-x86_64", {})
        data["launcher_update_url"] = platform_info.get("url", "")
    else:
        data["launcher_update_url"] = ""

    return success(data=data, msg="Launcher config retrieved")


@router.get("/launcher/update/{target}/{arch}/{current_version}")
async def check_launcher_update(target: str, arch: str, current_version: str):
    """Tauri 自更新检查接口

    - 有更新时返回 HTTP 200 + Tauri updater JSON
    - 无更新时返回 HTTP 204
    """
    data = _load_toml(Path(settings.launcher_update_path))

    latest_version, version_info = _resolve_latest(data)
    if not latest_version or not version_info:
        return Response(status_code=204)

    # 比较版本号，当前版本 >= 最新版本时无需更新
    try:
        if _parse_version(current_version) >= _parse_version(latest_version):
            return Response(status_code=204)
    except (ValueError, TypeError):
        logger.warning(f"Invalid version format: current={current_version}, latest={latest_version}")
        return Response(status_code=204)

    # 查找匹配的平台
    platform_key = f"{target}-{arch}"
    platforms: dict = version_info.get("platforms", {})
    platform_info = platforms.get(platform_key)

    if not platform_info:
        logger.info(f"No update available for platform: {platform_key}")
        return Response(status_code=204)

    # 返回 Tauri updater 格式
    return JSONResponse(
        content={
            "version": latest_version,
            "notes": version_info.get("notes", ""),
            "pub_date": version_info.get("pub_date", ""),
            "platforms": {platform_key: platform_info},
        }
    )
