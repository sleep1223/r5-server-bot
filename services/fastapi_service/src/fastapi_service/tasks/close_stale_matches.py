import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from shared_lib.config import settings
from shared_lib.models import Match

from fastapi_service.services.ingest_service import _ACTIVE_MATCH_BY_SERVER


async def close_stale_matches_task() -> None:
    """定时把超时未关闭的 active match 标记为 abandoned。

    正常路径由 ingest_service 通过 Prematch / 新 MatchSetup 关闭对局；
    本任务是 safety net（游戏服崩溃、LiveAPI 掉线等异常场景的兜底）。
    超时阈值建议 >= 两场典型 BR 时长 (~30min)。
    """
    interval = settings.match_closer_interval_seconds
    timeout_seconds = settings.match_inactivity_timeout_seconds
    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
            stale = await Match.filter(status="active", started_at__lt=cutoff).all()
            for m in stale:
                rows = await Match.filter(id=m.id, status="active").update(
                    status="abandoned",
                    ended_at=datetime.now(timezone.utc),
                    end_reason="inactivity",
                )
                _ACTIVE_MATCH_BY_SERVER.pop(m.server_id, None)
                if rows:
                    logger.info(f"Match abandon: id={m.id}, full_match_id={m.full_match_id}, started_at={m.started_at}")
        except Exception as e:
            logger.error(f"close_stale_matches_task 异常: {e}")
        await asyncio.sleep(interval)
