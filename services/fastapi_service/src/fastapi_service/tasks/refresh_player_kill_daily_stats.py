import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from loguru import logger
from shared_lib.config import settings
from tortoise.transactions import in_transaction

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_refresh_lock = asyncio.Lock()

_DELETE_WEAPON_SQL = """
DELETE FROM player_kill_daily_weapon_stats
WHERE stat_date >= $1::date
  AND stat_date <  $2::date
"""

_DELETE_OPPONENT_SQL = """
DELETE FROM player_kill_daily_opponent_stats
WHERE stat_date >= $1::date
  AND stat_date <  $2::date
"""

_INSERT_WEAPON_SQL = """
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
        COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') AS weapon,
        'unknown' AS input_device,
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
        COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') AS weapon,
        'unknown' AS input_device,
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
        COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') AS weapon,
        'unknown' AS input_device,
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
      AND pmws.opponent_id IS NOT NULL
      AND pmws.player_id <> pmws.opponent_id
      AND pmws.kills > 0
      AND NOT EXISTS (
          SELECT 1
          FROM player_killed pk
          WHERE pk.created_at >= b.start_ts
            AND pk.created_at <  b.end_ts
            AND pk.match_id = pmws.match_id
            AND pk.attacker_id = pmws.player_id
            AND pk.victim_id = pmws.opponent_id
            AND COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') = COALESCE(NULLIF(lower(trim(pmws.weapon)), ''), 'unknown')
      )

    UNION ALL

    SELECT
        (COALESCE(m.ended_at, m.started_at, pmws.created_at) AT TIME ZONE 'Asia/Shanghai')::date AS stat_date,
        pmws.server_id,
        pmws.opponent_id AS player_id,
        COALESCE(NULLIF(lower(trim(pmws.weapon)), ''), 'unknown') AS weapon,
        COALESCE(
            (
                SELECT NULLIF(replace(replace(lower(trim(pmws_victim.input_device)), '-', '_'), ' ', '_'), '')
                FROM player_match_weapon_stats pmws_victim
                WHERE pmws_victim.match_id = pmws.match_id
                  AND pmws_victim.player_id = pmws.opponent_id
                ORDER BY
                    CASE WHEN pmws_victim.source = 'sdk_match_end' THEN 0 ELSE 1 END,
                    CASE WHEN pmws_victim.opponent_id IS NULL THEN 0 ELSE 1 END,
                    pmws_victim.id DESC
                LIMIT 1
            ),
            (
                SELECT NULLIF(replace(replace(lower(trim(p.input_device)), '-', '_'), ' ', '_'), '')
                FROM players p
                WHERE p.id = pmws.opponent_id
            ),
            'unknown'
        ) AS input_device,
        0 AS kills,
        pmws.kills AS deaths,
        0 AS awarded_kills
    FROM player_match_weapon_stats pmws
    JOIN matches m ON m.id = pmws.match_id
    CROSS JOIN bounds b
    WHERE COALESCE(m.ended_at, m.started_at, pmws.created_at) >= b.start_ts
      AND COALESCE(m.ended_at, m.started_at, pmws.created_at) <  b.end_ts
      AND pmws.server_id IS NOT NULL
      AND pmws.player_id IS NOT NULL
      AND pmws.opponent_id IS NOT NULL
      AND pmws.player_id <> pmws.opponent_id
      AND pmws.kills > 0
      AND NOT EXISTS (
          SELECT 1
          FROM player_killed pk
          WHERE pk.created_at >= b.start_ts
            AND pk.created_at <  b.end_ts
            AND pk.match_id = pmws.match_id
            AND pk.attacker_id = pmws.player_id
            AND pk.victim_id = pmws.opponent_id
            AND COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') = COALESCE(NULLIF(lower(trim(pmws.weapon)), ''), 'unknown')
      )

    UNION ALL

    SELECT
        (COALESCE(m.ended_at, m.started_at, pmws.created_at) AT TIME ZONE 'Asia/Shanghai')::date AS stat_date,
        pmws.server_id,
        pmws.player_id,
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
      AND pmws.opponent_id IS NULL
      AND pmws.kills > 0
      AND NOT EXISTS (
          SELECT 1
          FROM player_match_weapon_stats pmws_detail
          WHERE pmws_detail.match_id = pmws.match_id
            AND pmws_detail.player_id = pmws.player_id
            AND pmws_detail.opponent_id IS NOT NULL
            AND COALESCE(NULLIF(lower(trim(pmws_detail.weapon)), ''), 'unknown') = COALESCE(NULLIF(lower(trim(pmws.weapon)), ''), 'unknown')
            AND COALESCE(pmws_detail.source, '') = COALESCE(pmws.source, '')
      )
      AND NOT EXISTS (
          SELECT 1
          FROM player_killed pk
          WHERE pk.created_at >= b.start_ts
            AND pk.created_at <  b.end_ts
            AND pk.match_id = pmws.match_id
            AND pk.attacker_id = pmws.player_id
            AND COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') = COALESCE(NULLIF(lower(trim(pmws.weapon)), ''), 'unknown')
      )
)
INSERT INTO player_kill_daily_weapon_stats (
    stat_date,
    server_id,
    player_id,
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
    weapon,
    input_device,
    SUM(kills)::int,
    SUM(deaths)::int,
    SUM(awarded_kills)::int,
    now()
FROM events
GROUP BY stat_date, server_id, player_id, weapon, input_device
"""

_INSERT_OPPONENT_SQL = """
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
        1 AS kills,
        0 AS deaths
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
        0 AS kills,
        1 AS deaths
    FROM player_killed pk, bounds b
    WHERE pk.created_at >= b.start_ts
      AND pk.created_at <  b.end_ts
      AND pk.server_id IS NOT NULL
      AND pk.attacker_id IS NOT NULL
      AND pk.victim_id IS NOT NULL
      AND pk.attacker_id <> pk.victim_id

    UNION ALL

    SELECT
        (COALESCE(m.ended_at, m.started_at, pmws.created_at) AT TIME ZONE 'Asia/Shanghai')::date AS stat_date,
        pmws.server_id,
        pmws.player_id,
        pmws.opponent_id,
        pmws.kills AS kills,
        0 AS deaths
    FROM player_match_weapon_stats pmws
    JOIN matches m ON m.id = pmws.match_id
    CROSS JOIN bounds b
    WHERE COALESCE(m.ended_at, m.started_at, pmws.created_at) >= b.start_ts
      AND COALESCE(m.ended_at, m.started_at, pmws.created_at) <  b.end_ts
      AND pmws.server_id IS NOT NULL
      AND pmws.player_id IS NOT NULL
      AND pmws.opponent_id IS NOT NULL
      AND pmws.player_id <> pmws.opponent_id
      AND pmws.kills > 0
      AND NOT EXISTS (
          SELECT 1
          FROM player_killed pk
          WHERE pk.created_at >= b.start_ts
            AND pk.created_at <  b.end_ts
            AND pk.match_id = pmws.match_id
            AND pk.attacker_id = pmws.player_id
            AND pk.victim_id = pmws.opponent_id
      )

    UNION ALL

    SELECT
        (COALESCE(m.ended_at, m.started_at, pmws.created_at) AT TIME ZONE 'Asia/Shanghai')::date AS stat_date,
        pmws.server_id,
        pmws.opponent_id AS player_id,
        pmws.player_id AS opponent_id,
        0 AS kills,
        pmws.kills AS deaths
    FROM player_match_weapon_stats pmws
    JOIN matches m ON m.id = pmws.match_id
    CROSS JOIN bounds b
    WHERE COALESCE(m.ended_at, m.started_at, pmws.created_at) >= b.start_ts
      AND COALESCE(m.ended_at, m.started_at, pmws.created_at) <  b.end_ts
      AND pmws.server_id IS NOT NULL
      AND pmws.player_id IS NOT NULL
      AND pmws.opponent_id IS NOT NULL
      AND pmws.player_id <> pmws.opponent_id
      AND pmws.kills > 0
      AND NOT EXISTS (
          SELECT 1
          FROM player_killed pk
          WHERE pk.created_at >= b.start_ts
            AND pk.created_at <  b.end_ts
            AND pk.match_id = pmws.match_id
            AND pk.attacker_id = pmws.player_id
            AND pk.victim_id = pmws.opponent_id
      )
)
INSERT INTO player_kill_daily_opponent_stats (
    stat_date,
    server_id,
    player_id,
    opponent_id,
    kills,
    deaths,
    refreshed_at
)
SELECT
    stat_date,
    server_id,
    player_id,
    opponent_id,
    SUM(kills)::int,
    SUM(deaths)::int,
    now()
FROM events
GROUP BY stat_date, server_id, player_id, opponent_id
"""

_INSERT_SQL = _INSERT_WEAPON_SQL + "\n\n" + _INSERT_OPPONENT_SQL


def _today_shanghai() -> date:
    return datetime.now(_SHANGHAI_TZ).date()


async def refresh_player_kill_daily_stats_window(start_day: date, end_day: date) -> None:
    """Rebuild player kill daily weapon/opponent stats for [start_day, end_day)."""
    if start_day >= end_day:
        logger.warning(f"跳过玩家击杀日统计刷新: 窗口无效 {start_day}..{end_day}")
        return

    async with _refresh_lock:
        async with in_transaction() as conn:
            await conn.execute_query(_DELETE_WEAPON_SQL, [start_day, end_day])
            await conn.execute_query(_DELETE_OPPONENT_SQL, [start_day, end_day])
            await conn.execute_query(_INSERT_WEAPON_SQL, [start_day, end_day])
            await conn.execute_query(_INSERT_OPPONENT_SQL, [start_day, end_day])
    logger.info(f"已刷新玩家击杀日统计窗口: {start_day}..{end_day}")


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
