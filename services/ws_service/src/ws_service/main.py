import asyncio
import logging
import sys

from loguru import logger
from shared_lib.config import settings

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


def _configure_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:HH:mm:ss}</green> "
            "<level>{level: <7}</level> "
            "<level>{message}</level>"
        ),
        level="INFO",
        colorize=True,
    )
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.INFO, force=True)


async def main() -> None:
    _configure_logging()
    listener = LiveAPIListener(
        host=settings.ws_host,
        port=settings.ws_port,
        batch_interval=settings.ws_batch_interval,
        batch_max_retries=settings.ws_batch_max_retries,
        buffer_max=settings.ws_ingest_buffer_max,
    )
    await listener.start()


if __name__ == "__main__":
    asyncio.run(main())
