import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from loguru import logger
from shared_lib.config import settings
from tortoise.transactions import in_transaction

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_refresh_lock = asyncio.Lock()

_DELETE_SQL = """
DELETE FROM player_kill_daily_stats
WHERE stat_date >= $1::date
  AND stat_date <  $2::date
"""

_INSERT_SQL = """
WITH bounds AS (
    SELECT
        ($1::date::timestamp AT TIME ZONE 'Asia/Shanghai') AS start_ts,
        ($2::date::timestamp AT TIME ZONE 'Asia/Shanghai') AS end_ts
),
events AS (
    SELECT
        (pk.created_at AT TIME ZONE 'Asia/Shanghai')::date AS stat_date,
        pk.server_id,
        pk.attacker_id AS player_id,
        1 AS kills,
        0 AS deaths,
        0 AS awarded_kills
    FROM player_killed pk, bounds b
    WHERE pk.created_at >= b.start_ts
      AND pk.created_at <  b.end_ts
      AND pk.server_id IS NOT NULL
      AND pk.attacker_id IS NOT NULL
      AND pk.victim_id IS NOT NULL
      AND pk.attacker_id <> pk.victim_id

    UNION ALL

    SELECT
        (pk.created_at AT TIME ZONE 'Asia/Shanghai')::date AS stat_date,
        pk.server_id,
        pk.victim_id AS player_id,
        0 AS kills,
        1 AS deaths,
        0 AS awarded_kills
    FROM player_killed pk, bounds b
    WHERE pk.created_at >= b.start_ts
      AND pk.created_at <  b.end_ts
      AND pk.server_id IS NOT NULL
      AND pk.attacker_id IS NOT NULL
      AND pk.victim_id IS NOT NULL
      AND pk.attacker_id <> pk.victim_id

    UNION ALL

    SELECT
        (pk.created_at AT TIME ZONE 'Asia/Shanghai')::date AS stat_date,
        pk.server_id,
        pk.awarded_to_id AS player_id,
        0 AS kills,
        0 AS deaths,
        1 AS awarded_kills
    FROM player_killed pk, bounds b
    WHERE pk.created_at >= b.start_ts
      AND pk.created_at <  b.end_ts
      AND pk.server_id IS NOT NULL
      AND pk.awarded_to_id IS NOT NULL
      AND pk.attacker_id IS NOT NULL
      AND pk.victim_id IS NOT NULL
      AND pk.attacker_id <> pk.victim_id
)
INSERT INTO player_kill_daily_stats (
    stat_date,
    server_id,
    player_id,
    kills,
    deaths,
    awarded_kills,
    refreshed_at
)
SELECT
    stat_date,
    server_id,
    player_id,
    SUM(kills)::int,
    SUM(deaths)::int,
    SUM(awarded_kills)::int,
    now()
FROM events
GROUP BY stat_date, server_id, player_id
"""


def _today_shanghai() -> date:
    return datetime.now(_SHANGHAI_TZ).date()


async def refresh_player_kill_daily_stats_window(start_day: date, end_day: date) -> None:
    """Rebuild player kill daily stats for [start_day, end_day)."""
    if start_day >= end_day:
        logger.warning(f"skip player kill daily stats refresh: invalid window {start_day}..{end_day}")
        return

    async with _refresh_lock:
        async with in_transaction() as conn:
            await conn.execute_query(_DELETE_SQL, [start_day, end_day])
            await conn.execute_query(_INSERT_SQL, [start_day, end_day])
    logger.info(f"refreshed player_kill_daily_stats window: {start_day}..{end_day}")


async def player_kill_daily_stats_refresh_task() -> None:
    interval = max(1, settings.player_kill_daily_stats_refresh_interval_seconds)
    lookback_days = max(1, settings.player_kill_daily_stats_refresh_lookback_days)

    while True:
        try:
            end_day = _today_shanghai() + timedelta(days=1)
            start_day = end_day - timedelta(days=lookback_days)
            await refresh_player_kill_daily_stats_window(start_day, end_day)
        except Exception as e:
            logger.error(f"player_kill_daily_stats_refresh_task 异常: {e}")
        await asyncio.sleep(interval)
