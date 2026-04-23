import asyncio
import logging
import sys

from loguru import logger
from shared_lib.config import settings

from .dashboard import dashboard_live, is_interactive
from .display import state
from .listener import LiveAPIListener


class _InterceptHandler(logging.Handler):
    """把标准库 logging 的输出转发到 loguru，避免 websockets 等库的日志走两套格式。"""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _sink_milestone(message) -> None:
    """WARNING/ERROR loguru 消息 → 推进 dashboard milestones，避免 stderr 打断画面。"""
    record = message.record
    level = record["level"].name
    icon = "⚠️" if level == "WARNING" else "🛑"
    state.push_milestone(icon, level, record["message"])


def _configure_logging() -> None:
    logger.remove()
    if is_interactive():
        # TTY 下 stderr 会被 rich.Live 持续覆盖，把 loguru 路由进仪表盘 milestones
        logger.add(_sink_milestone, level="WARNING", format="{message}")
        logger.add(
            "logs/ws_service.log",
            level="DEBUG",
            rotation="50 MB",
            retention=5,
            enqueue=True,
            format="{time:YYYY-MM-DD HH:mm:ss} {level: <7} {message}",
        )
    else:
        # 非 TTY（systemd/nohup）走原来的滚动模式
        logger.add(
            sys.stderr,
            format=("<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> <level>{message}</level>"),
            level="INFO",
            colorize=True,
        )
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.INFO, force=True)


async def _run() -> None:
    listener = LiveAPIListener(
        host=settings.ws_host,
        port=settings.ws_port,
        batch_interval=settings.ws_batch_interval,
        batch_max_retries=settings.ws_batch_max_retries,
        buffer_max=settings.ws_ingest_buffer_max,
    )
    async with dashboard_live():
        await listener.start()


async def main() -> None:
    _configure_logging()
    try:
        await _run()
    except KeyboardInterrupt:
        logger.info("收到中断信号，退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
