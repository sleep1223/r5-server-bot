"""rich.Live 仪表盘驱动器。

- 在 TTY 环境下用 Live 原地刷新 DashboardState.render()
- 非 TTY（systemd/nohup/重定向）自动降级：不启动 Live，loguru 保持滚动输出
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from loguru import logger
from rich.live import Live

from .display import DASHBOARD_REFRESH_PER_SEC, console, state


def is_interactive() -> bool:
    return console.is_terminal


async def _refresh_loop(live: Live) -> None:
    interval = 1.0 / DASHBOARD_REFRESH_PER_SEC
    try:
        while True:
            live.update(state.render())
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        return


@asynccontextmanager
async def dashboard_live() -> AsyncIterator[None]:
    """TTY 下运行 Live 仪表盘的上下文管理器。非 TTY 直接 yield，不启用 Live。"""
    if not is_interactive():
        logger.info("非 TTY 环境，使用滚动日志模式（未启用仪表盘）")
        yield
        return

    with Live(
        state.render(),
        console=console,
        refresh_per_second=DASHBOARD_REFRESH_PER_SEC,
        screen=False,
        transient=False,
    ) as live:
        task = asyncio.create_task(_refresh_loop(live))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except Exception:
                pass
            live.update(state.render())
