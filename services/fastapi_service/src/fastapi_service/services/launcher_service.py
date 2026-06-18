import copy
import tomllib
from pathlib import Path
from typing import Any

from loguru import logger
from shared_lib.config import settings

from fastapi_service.tasks.fetch_launcher_version import launcher_version_cache


class LauncherConfigError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _load_toml(path: Path) -> dict[str, Any]:
    """读取并解析 TOML 文件"""
    if not path.exists():
        logger.error(f"配置文件不存在: {path}")
        raise LauncherConfigError(404, f"配置文件不存在: {path.name}")
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        logger.error(f"配置文件解析失败: {e}")
        raise LauncherConfigError(500, "配置文件无效") from e


def _parse_version(v: str) -> tuple[int, ...]:
    """将版本号字符串解析为整数元组，用于比较。如 '0.4.0' -> (0, 4, 0)"""
    return tuple(int(x) for x in v.strip().lstrip("v").split("."))


def _normalize_patches(config: dict[str, Any]) -> list[dict[str, Any]]:
    patches = config.get("patches")
    if not isinstance(patches, list):
        return []

    normalized: list[dict[str, Any]] = []
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        normalized.append({
            "from_version": str(patch.get("from_version") or ""),
            "to_version": str(patch.get("to_version") or ""),
            "url": str(patch.get("url") or ""),
            "checksum": str(patch.get("checksum") or ""),
            "size": int(patch.get("size") or 0),
        })

    return normalized


def _resolve_latest(data: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """解析最新版本号和对应的版本信息，如果 latest 在 versions 中找不到则 fallback 到第一个条目并替换版本号。

    版本号优先来源：GitHub Releases 缓存（由 fetch_launcher_version_task 维护），
    缺失时回退到 TOML 里的 `latest` 字段。
    """
    latest_version = launcher_version_cache.get() or data.get("latest", "")
    versions: list[dict[str, Any]] = data.get("versions", [])
    if not latest_version and not versions:
        return "", None
    version_info = next((v for v in versions if v.get("version") == latest_version), None)
    if not version_info and versions:
        fallback = versions[0]
        fallback_version = fallback.get("version", "")
        logger.warning(f"配置的 latest 版本 '{latest_version}' 不在 versions 列表中，从 '{fallback_version}' 回退")
        version_info = copy.deepcopy(fallback)
        version_info["version"] = latest_version
        # 把 platforms.*.url 中出现的旧版本号替换为新版本号，使下载链接指向 GitHub Releases 上的新版本资产
        if fallback_version:
            platforms = version_info.get("platforms", {})
            if isinstance(platforms, dict):
                for plat in platforms.values():
                    if isinstance(plat, dict):
                        url = plat.get("url")
                        if isinstance(url, str) and url:
                            plat["url"] = url.replace(fallback_version, latest_version)
                        # 旧签名对新资产无效，置空避免误导客户端校验
                        if "signature" in plat:
                            plat["signature"] = ""
    return latest_version, version_info


def get_launcher_config() -> dict[str, Any]:
    config = _load_toml(Path(settings.launcher_config_path))
    update_data = _load_toml(Path(settings.launcher_update_path))
    latest_version, version_info = _resolve_latest(update_data)

    data = {
        "offline_package_url": str(config.get("offline_package_url") or ""),
        "download_domain": str(config.get("download_domain") or ""),
        "docs_url": str(config.get("docs_url") or ""),
        "launcher_version": latest_version,
        "launcher_update_url": str(config.get("launcher_update_url") or ""),
        "force_update": bool(config.get("force_update", False)),
        "game_version": str(config.get("game_version") or ""),
        "patches": _normalize_patches(config),
        "announcement": config.get("announcement") or {},
        "rules": config.get("rules") or [],
    }

    if version_info:
        platform_info = version_info.get("platforms", {}).get("windows-x86_64", {})
        if isinstance(platform_info, dict):
            data["launcher_update_url"] = platform_info.get("url", "")

    return data


def get_launcher_update(target: str, arch: str, current_version: str) -> dict[str, Any] | None:
    update_data = _load_toml(Path(settings.launcher_update_path))

    latest_version, version_info = _resolve_latest(update_data)
    if not latest_version or not version_info:
        return None

    try:
        if _parse_version(current_version) >= _parse_version(latest_version):
            return None
    except (ValueError, TypeError):
        logger.warning(f"版本格式无效: current={current_version}, latest={latest_version}")
        return None

    platform_key = f"{target}-{arch}"
    platforms = version_info.get("platforms", {})
    platform_info = platforms.get(platform_key) if isinstance(platforms, dict) else None

    if not platform_info:
        logger.info(f"平台暂无可用更新: {platform_key}")
        return None

    return {
        "version": latest_version,
        "notes": version_info.get("notes", ""),
        "pub_date": version_info.get("pub_date", ""),
        "platforms": {platform_key: platform_info},
    }
