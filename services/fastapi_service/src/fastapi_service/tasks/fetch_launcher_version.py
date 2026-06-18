import asyncio

import httpx
from loguru import logger
from shared_lib.config import settings


class _LauncherVersionCache:
    def __init__(self) -> None:
        self._version: str = ""

    def set(self, version: str) -> None:
        self._version = version

    def get(self) -> str:
        return self._version


launcher_version_cache = _LauncherVersionCache()


async def fetch_launcher_version_task() -> None:
    """定时从 GitHub Releases 拉取启动器最新版本号，写入进程内缓存。"""
    repo = (settings.launcher_github_repo or "").strip()
    interval = max(int(settings.launcher_github_fetch_interval or 600), 60)
    if not repo:
        logger.info("未配置 Launcher GitHub 仓库，跳过版本拉取任务")
        return
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    logger.info(f"Launcher 版本拉取任务已启动: url={url}, interval={interval}s")
    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=10.0, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code == 200:
                data = resp.json()
                tag = str(data.get("tag_name") or "").strip().lstrip("v")
                if tag:
                    if tag != launcher_version_cache.get():
                        logger.info(f"Launcher 最新版本已更新: {tag}")
                    launcher_version_cache.set(tag)
                else:
                    logger.warning("GitHub release 返回了空 tag_name")
            else:
                logger.warning(f"拉取 Launcher 版本失败: {resp.status_code}")
        except Exception as e:
            logger.error(f"拉取 Launcher 版本异常: {e}")
        await asyncio.sleep(interval)
