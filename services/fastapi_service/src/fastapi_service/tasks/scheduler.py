import asyncio

from loguru import logger

from fastapi_service.services.binding_role_service import apply_configured_roles

from .fetch_apex import fetch_apex_cache_task
from .fetch_launcher_version import fetch_launcher_version_task
from .fetch_servers import fetch_server_list_raw_once, fetch_server_list_raw_task
from .refresh_player_kill_daily_stats import player_kill_daily_stats_refresh_task
from .resolve_ips import ip_resolution_task
from .sync_milky_admins import milky_admin_sync_task


class TaskScheduler:
    """管理所有后台任务的生命周期。"""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        configured_role_summary = await apply_configured_roles()
        logger.info(f"已应用配置管理员 QQ: {configured_role_summary}")
        logger.info("启动后台任务前先拉取初始服务器列表")
        initial_server_count = await fetch_server_list_raw_once()
        if initial_server_count is None:
            logger.warning("初始服务器列表拉取失败，将等待周期性拉取任务")

        self._tasks = [
            asyncio.create_task(fetch_server_list_raw_task(delay_first=initial_server_count is not None)),
            asyncio.create_task(ip_resolution_task()),
            asyncio.create_task(fetch_launcher_version_task()),
            asyncio.create_task(player_kill_daily_stats_refresh_task()),
            asyncio.create_task(fetch_apex_cache_task()),
            asyncio.create_task(milky_admin_sync_task()),
        ]
        logger.info(f"已启动 {len(self._tasks)} 个后台任务")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                logger.error(f"后台任务关闭时异常: {r}")
        self._tasks.clear()
        logger.info("所有后台任务已停止")


task_scheduler = TaskScheduler()
