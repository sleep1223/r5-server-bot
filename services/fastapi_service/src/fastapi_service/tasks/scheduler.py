import asyncio

from loguru import logger

from .close_stale_matches import close_stale_matches_task
from .fetch_servers import fetch_server_list_raw_task
from .reconcile_matches import reconcile_matches_task
from .resolve_ips import ip_resolution_task
from .sync_players import sync_players_task


class TaskScheduler:
    """管理所有后台任务的生命周期。"""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(sync_players_task()),
            asyncio.create_task(fetch_server_list_raw_task()),
            asyncio.create_task(ip_resolution_task()),
            asyncio.create_task(close_stale_matches_task()),
            asyncio.create_task(reconcile_matches_task()),
        ]
        logger.info(f"Started {len(self._tasks)} background tasks")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                logger.error(f"Background task error during shutdown: {r}")
        self._tasks.clear()
        logger.info("All background tasks stopped")


task_scheduler = TaskScheduler()
