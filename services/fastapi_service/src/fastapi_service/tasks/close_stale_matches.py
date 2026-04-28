import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from shared_lib.config import settings
from shared_lib.models import Match, PlayerKilled


async def _close_one(match: Match, ended_at: datetime, *, status: str, reason: str) -> bool:
    """CAS 关闭指定 match；返回是否更新成功。

    注意：本任务跑在主 app 进程，而 `_ACTIVE_MATCH_BY_SERVER` 缓存在 ingest 进程，
    跨进程不共享。这里只依赖 DB CAS；ingest 进程的缓存会在下一次事件到达时
    通过 `_load_active_match` 回源 DB 自我修正。
    """
    rows = await Match.filter(id=match.id, status="active").update(
        status=status,
        ended_at=ended_at,
        end_reason=reason,
    )
    if rows:
        logger.info(
            f"Match {status}: id={match.id}, full_match_id={match.full_match_id}, "
            f"reason={reason}, started_at={match.started_at}"
        )
    return bool(rows)


async def _close_no_activity_matches(now: datetime) -> None:
    """无击杀活动超过阈值 → 标记为 completed/no_activity。

    覆盖"玩家全退 → 游戏服不再 emit Prematch"这类场景。给一段宽限期
    (started_at < now - N **且** created_at < now - N) 避免把刚写入但 started_at
    回溯的 back-dated match（比如 ws_service 重启后 replay 的旧 MatchSetup）秒死。
    """
    grace = timedelta(seconds=settings.match_no_activity_timeout_seconds)
    cutoff = now - grace
    candidates = await Match.filter(
        status="active",
        started_at__lt=cutoff,
        created_at__lt=cutoff,
    ).all()
    if not candidates:
        return

    candidate_ids = [m.id for m in candidates]
    # 一次 GROUP BY 判出谁还有近活动，避开 N+1
    recent_kill_rows = (
        await PlayerKilled.filter(match_id__in=candidate_ids, created_at__gte=cutoff)
        .distinct()
        .values_list("match_id", flat=True)
    )
    has_recent = {mid for mid in recent_kill_rows if mid is not None}

    for m in candidates:
        if m.id in has_recent:
            continue
        await _close_one(m, now, status="completed", reason="no_activity")


async def _close_hard_timeout_matches(now: datetime) -> None:
    """总时长 safety net（默认 2h）→ 标记为 abandoned/inactivity。

    同样要求 started_at 和 created_at 都早于 cutoff，避免 back-dated MatchSetup
    （ws_service 重启后 replay）刚插入就被秒判超时。
    """
    cutoff = now - timedelta(seconds=settings.match_inactivity_timeout_seconds)
    stale = await Match.filter(
        status="active",
        started_at__lt=cutoff,
        created_at__lt=cutoff,
    ).all()
    for m in stale:
        await _close_one(m, now, status="abandoned", reason="inactivity")


async def close_stale_matches_task() -> None:
    """定时关闭超时/无活动的 active match。

    两道门，顺序不能颠倒：
    1. 先看"无击杀活动 > match_no_activity_timeout_seconds"（默认 30min）→ completed/no_activity
    2. 再看"总时长 > match_inactivity_timeout_seconds"（默认 2h）→ abandoned/inactivity

    正常路径仍由 ingest_service 通过 Prematch / 新 MatchSetup 关闭。
    """
    interval = settings.match_closer_interval_seconds
    while True:
        try:
            now = datetime.now(timezone.utc)
            await _close_no_activity_matches(now)
            await _close_hard_timeout_matches(now)
        except Exception as e:
            logger.error(f"close_stale_matches_task 异常: {e}")
        await asyncio.sleep(interval)
