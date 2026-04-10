import asyncio

import httpx
from loguru import logger

from fastapi_service.core.cache import server_cache


async def fetch_server_list_raw_task() -> None:
    url = "https://r5r-sl.ugniushosting.com/servers"
    while True:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    server_cache.update_raw_response(data)
                else:
                    server_cache.update_raw_response({"data": data})
            else:
                logger.warning(f"Failed to fetch raw server list: {response.status_code}")
        except Exception as e:
            logger.error(f"Error fetching raw server list: {e}")
        await asyncio.sleep(5)
