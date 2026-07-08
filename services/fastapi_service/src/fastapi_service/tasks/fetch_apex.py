import asyncio

from loguru import logger
from shared_lib.config import settings

from fastapi_service.services.apex_service import refresh_all_cached_resources


async def fetch_apex_cache_task() -> None:
    """定时刷新 Apex 地图轮换、官方服务器状态和顶猎分数缓存。"""
    interval = max(int(settings.apex_cache_refresh_interval or 600), 60)
    logger.info(f"Apex 缓存刷新任务已启动: interval={interval}s")

    while True:
        await refresh_all_cached_resources()
        await asyncio.sleep(interval)
