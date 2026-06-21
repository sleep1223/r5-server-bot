import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from loguru import logger
from shared_lib.config import settings
from tortoise.transactions import in_transaction

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_refresh_lock = asyncio.Lock()

_DELETE_SQL = """
DELETE FROM player_kill_daily_weapon_opponent_stats
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
        pk.victim_id AS opponent_id,
        COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') AS weapon,
        'unknown'::text AS input_device,
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
        pk.attacker_id AS opponent_id,
        COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') AS weapon,
        'unknown'::text AS input_device,
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
        pk.victim_id AS opponent_id,
        COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') AS weapon,
        'unknown'::text AS input_device,
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

    UNION ALL

    SELECT
        (COALESCE(m.ended_at, m.started_at, pmws.created_at) AT TIME ZONE 'Asia/Shanghai')::date AS stat_date,
        pmws.server_id,
        pmws.player_id,
        NULL::int AS opponent_id,
        COALESCE(NULLIF(lower(trim(pmws.weapon)), ''), 'unknown') AS weapon,
        COALESCE(NULLIF(lower(trim(pmws.input_device)), ''), 'unknown') AS input_device,
        pmws.kills AS kills,
        0 AS deaths,
        0 AS awarded_kills
    FROM player_match_weapon_stats pmws
    JOIN matches m ON m.id = pmws.match_id
    CROSS JOIN bounds b
    WHERE COALESCE(m.ended_at, m.started_at, pmws.created_at) >= b.start_ts
      AND COALESCE(m.ended_at, m.started_at, pmws.created_at) <  b.end_ts
      AND pmws.server_id IS NOT NULL
      AND pmws.player_id IS NOT NULL
      AND pmws.kills > 0
      AND NOT EXISTS (
          SELECT 1
          FROM player_killed pk_existing
          WHERE pk_existing.match_id = pmws.match_id
            AND pk_existing.created_at >= b.start_ts - interval '2 hours'
            AND pk_existing.created_at <  b.end_ts + interval '30 minutes'
            AND pk_existing.attacker_id IS NOT NULL
      )
)
INSERT INTO player_kill_daily_weapon_opponent_stats (
    stat_date,
    server_id,
    player_id,
    opponent_id,
    weapon,
    input_device,
    kills,
    deaths,
    awarded_kills,
    refreshed_at
)
SELECT
    stat_date,
    server_id,
    player_id,
    opponent_id,
    weapon,
    input_device,
    SUM(kills)::int,
    SUM(deaths)::int,
    SUM(awarded_kills)::int,
    now()
FROM events
GROUP BY stat_date, server_id, player_id, opponent_id, weapon, input_device
"""


def _today_shanghai() -> date:
    return datetime.now(_SHANGHAI_TZ).date()


async def refresh_player_kill_daily_stats_window(start_day: date, end_day: date) -> None:
    """Rebuild player kill daily weapon/opponent stats for [start_day, end_day)."""
    if start_day >= end_day:
        logger.warning(f"跳过玩家击杀日统计刷新: 窗口无效 {start_day}..{end_day}")
        return

    async with _refresh_lock:
        async with in_transaction() as conn:
            await conn.execute_query(_DELETE_SQL, [start_day, end_day])
            await conn.execute_query(_INSERT_SQL, [start_day, end_day])
    logger.info(f"已刷新 player_kill_daily_weapon_opponent_stats 窗口: {start_day}..{end_day}")


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
