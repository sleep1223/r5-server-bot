import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from shared_lib.config import settings
from shared_lib.models import GameStateChanged, Match, PlayerKilled

from fastapi_service.services.ingest_service import (
    _synthesize_match,
    _ts_to_dt,
)


async def reconcile_matches_task() -> None:
    """对账任务：为 state 流显示"正在打"但 DB 无 active match 的 server 补一条 Match。

    事件驱动路径（`_dispatch_event` 里的 Playing 合成）覆盖正常流。这条是兜底：
    - 同批次内 Playing 在 MatchSetup 之前到达，合成失败（继承源还没写入）
    - 事件丢失 / ws 掉线期内状态已切到 Playing
    - 更早的历史数据 game_state 有 Playing 但 matches 空白

    判据：
      服务器最近一条 GameStateChanged 是 Playing，created_at > N min 前（给事件
      路径足够时间处理），且该 server 当前无 active match → 尝试合成。
    """
    interval = settings.match_reconcile_interval_seconds
    grace = timedelta(seconds=settings.match_reconcile_grace_seconds)
    while True:
        try:
            await _run_once(datetime.now(timezone.utc) - grace)
        except Exception as e:
            logger.error(f"reconcile_matches_task 异常: {e}")
        await asyncio.sleep(interval)


async def _run_once(grace_cutoff: datetime) -> None:
    # 当前 server → active match_id 的镜像（从 DB 查，不走内存缓存避免与 ingest 进程跨进程不同步）
    active_rows = await Match.filter(status="active").values("server_id", "id")
    active_by_server = {r["server_id"]: r["id"] for r in active_rows}

    # 每个 server 最近一条 GameStateChanged
    # Tortoise 没方便的 distinct on；用 Python 端按 server_id 取最新一条
    recent_rows = (
        await GameStateChanged.filter(server_id__isnull=False, created_at__lt=grace_cutoff)
        .order_by("-id")
        .limit(2000)
        .values("server_id", "state", "timestamp", "created_at")
    )
    latest_by_server: dict[int, dict] = {}
    for row in recent_rows:
        sid = row["server_id"]
        if sid not in latest_by_server:
            latest_by_server[sid] = row

    from shared_lib.models import Server

    for sid, row in latest_by_server.items():
        try:
            if row["state"] != "Playing":
                continue
            if sid in active_by_server:
                continue
            # 最近 30min 内该 server 有过击杀活动才值得补；避免给老的空 server 制造幻影 match
            recent_kill = await PlayerKilled.filter(
                server_id=sid, created_at__gte=grace_cutoff
            ).exists()
            if not recent_kill:
                continue
            # 已经为这条 Playing 合成过 match（可能已被 close_stale_matches 关掉）→ 跳过，
            # 否则会反复用同一个 ts 重复合成，撞 full_match_id 死循环直到 5 次耗尽抛错
            already_synthed = await Match.filter(
                server_id=sid, started_at=_ts_to_dt(row["timestamp"])
            ).exists()
            if already_synthed:
                continue
            server = await Server.get_or_none(id=sid)
            if not server:
                continue
            match = await _synthesize_match(server, row["timestamp"])
            if match:
                # 注：本任务在主 app 进程，无法直接更新 ingest 进程的
                # _ACTIVE_MATCH_BY_SERVER 缓存。ingest 进程下次事件触发
                # `_load_active_match` 时会从 DB 回源修正。
                logger.info(
                    f"reconcile: synth match id={match.id} server_id={sid} "
                    f"(last game_state=Playing @ {row['created_at']})"
                )
        except Exception as e:
            logger.error(f"reconcile: server_id={sid} 处理失败: {e}")
