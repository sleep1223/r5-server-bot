"""Ingest 子服务 Granian 启动器。

独立进程跑 ingest_main:app，workers 强制 1：batch_id 去重 LRU + 批次 asyncio.Lock
都靠"进程内唯一"来保证语义，多 worker 会破坏去重与玩家状态一致性。
ASGI 下 blocking_threads 固定为 1，并发全部靠 asyncio。
"""

from granian import Granian
from granian.constants import Interfaces
from granian.log import LogLevels
from shared_lib.config import settings


def main() -> None:
    server = Granian(
        target="fastapi_service.ingest_main:app",
        address=settings.fastapi_ingest_host,
        port=settings.fastapi_ingest_port,
        workers=1,
        reload=False,
        interface=Interfaces.ASGI,
        log_level=LogLevels.info,
        respawn_failed_workers=True,
        respawn_interval=3.5,
        workers_kill_timeout=30,
        workers_lifetime=3600 * 4,
        workers_max_rss=512,
        backpressure=settings.fastapi_backpressure,
        backlog=settings.fastapi_backlog,
    )
    server.serve()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ...
