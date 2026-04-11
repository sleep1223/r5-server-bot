import asyncio

import httpx
from loguru import logger
from shared_lib.config import settings

from fastapi_service.core.cache import server_cache


async def fetch_server_list_raw_task() -> None:
    """定时拉取远程服务器列表，并缓存到 server_cache.raw_response。

    拉取地址来源于 settings.r5_servers_url，拉取间隔来源于
    settings.r5_servers_fetch_interval（秒，默认 180s = 3 分钟）。
    """
    url = settings.r5_servers_url
    interval = max(int(settings.r5_servers_fetch_interval or 180), 1)
    logger.info(f"Raw server list fetch task started: url={url}, interval={interval}s")
    while True:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    server_cache.update_raw_response(data)
                else:
                    server_cache.update_raw_response({"servers": data})
            else:
                logger.warning(f"Failed to fetch raw server list: {response.status_code}")
        except Exception as e:
            logger.error(f"Error fetching raw server list: {e}")
        await asyncio.sleep(interval)
