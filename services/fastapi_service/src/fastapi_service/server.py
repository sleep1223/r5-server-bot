"""主 API Granian 启动器。

约定 workers=1 —— 进程内缓存（server_cache 等）与后台任务（拉服务器列表、
同步 IP、玩家同步）都依赖单进程共享状态，多 worker 会裂脑 / 重复执行。
ASGI 下 Granian 固定 blocking_threads=1（单事件循环），并发靠协程承载。
"""

from granian import Granian
from granian.constants import Interfaces
from granian.log import LogLevels
from shared_lib.config import settings


def main() -> None:
    server = Granian(
        target="fastapi_service.main:app",
        address=settings.fastapi_host,
        port=settings.fastapi_port,
        workers=1,
        reload=False,
        interface=Interfaces.ASGI,
        log_level=LogLevels.info,
        # ── Worker 健壮性 ──
        respawn_failed_workers=True,
        respawn_interval=3.5,
        workers_kill_timeout=30,
        # ── 内存保护 ──
        workers_lifetime=3600 * 4,
        workers_max_rss=512,
        # ── 背压控制 ──
        backpressure=settings.fastapi_backpressure,
        backlog=settings.fastapi_backlog,
    )
    server.serve()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ...
