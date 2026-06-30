# 玩家每日武器/对手统计拆表迁移 SQL

目标：用两张低基数缓存表替代 `player_kill_daily_weapon_opponent_stats`。

- `player_kill_daily_weapon_stats`: 日期 × 服务器 × 玩家 × 武器 × 输入设备，用于 `/kd`、武器榜、个人武器、组队 KD。
- `player_kill_daily_opponent_stats`: 日期 × 服务器 × 玩家 × 对手，用于 `/个人kd`。

回填口径：`player_killed` 优先，`player_match_weapon_stats` 只补没有原始击杀事件覆盖的数据。执行前建议停止 FastAPI 服务或至少停掉日统计刷新任务。下面只创建/回填新表，不会改旧表；旧表先保留，等确认不再需要回滚或审计后再手动删除释放空间。

```sql
CREATE TABLE IF NOT EXISTS player_kill_daily_weapon_stats (
    id bigserial PRIMARY KEY,
    stat_date date NOT NULL,
    server_id integer NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    player_id integer NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    weapon varchar(100) NOT NULL DEFAULT 'unknown',
    input_device varchar(50) NOT NULL DEFAULT 'unknown',
    kills integer NOT NULL DEFAULT 0,
    deaths integer NOT NULL DEFAULT 0,
    awarded_kills integer NOT NULL DEFAULT 0,
    refreshed_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS "uid_player_kill_stat_da_76ce3d"
    ON player_kill_daily_weapon_stats (
        stat_date,
        server_id,
        player_id,
        weapon,
        input_device
    );

CREATE INDEX IF NOT EXISTS "idx_player_kill_stat_da_0057f3"
    ON player_kill_daily_weapon_stats (stat_date);

CREATE INDEX IF NOT EXISTS "idx_player_kill_weapon_50cb76"
    ON player_kill_daily_weapon_stats (weapon);

CREATE INDEX IF NOT EXISTS "idx_player_kill_input_d_c9b72b"
    ON player_kill_daily_weapon_stats (input_device);

CREATE INDEX IF NOT EXISTS "idx_player_kill_player__f6d6c2"
    ON player_kill_daily_weapon_stats (player_id, stat_date, weapon);

CREATE INDEX IF NOT EXISTS "idx_player_kill_stat_da_e4ae20"
    ON player_kill_daily_weapon_stats (stat_date, weapon, player_id);

CREATE INDEX IF NOT EXISTS "idx_player_kill_stat_da_c0bb56"
    ON player_kill_daily_weapon_stats (stat_date, input_device, kills);

CREATE INDEX IF NOT EXISTS "idx_player_kill_stat_da_44e22c"
    ON player_kill_daily_weapon_stats (stat_date, kills);

CREATE INDEX IF NOT EXISTS "idx_player_kill_server__544be2"
    ON player_kill_daily_weapon_stats (server_id, stat_date, player_id);

CREATE TABLE IF NOT EXISTS player_kill_daily_opponent_stats (
    id bigserial PRIMARY KEY,
    stat_date date NOT NULL,
    server_id integer NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    player_id integer NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    opponent_id integer NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    kills integer NOT NULL DEFAULT 0,
    deaths integer NOT NULL DEFAULT 0,
    refreshed_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS "uid_player_kill_stat_da_3b6b16"
    ON player_kill_daily_opponent_stats (
        stat_date,
        server_id,
        player_id,
        opponent_id
    );

CREATE INDEX IF NOT EXISTS "idx_player_kill_stat_da_4100c1"
    ON player_kill_daily_opponent_stats (stat_date);

CREATE INDEX IF NOT EXISTS "idx_player_kill_player__e42faa"
    ON player_kill_daily_opponent_stats (player_id, stat_date, opponent_id);

CREATE INDEX IF NOT EXISTS "idx_player_kill_server__b23a57"
    ON player_kill_daily_opponent_stats (server_id, stat_date, player_id);

CREATE INDEX IF NOT EXISTS "idx_player_kill_stat_da_ec871f"
    ON player_kill_daily_opponent_stats (stat_date, player_id);
```

如需重跑全量回填，先清空新表：

```sql
TRUNCATE TABLE player_kill_daily_weapon_stats;
TRUNCATE TABLE player_kill_daily_opponent_stats;
```

回填武器表：

```sql
WITH events AS (
    SELECT
        (pk.created_at AT TIME ZONE 'Asia/Shanghai')::date AS stat_date,
        pk.server_id,
        pk.attacker_id AS player_id,
        COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') AS weapon,
        'unknown' AS input_device,
        1 AS kills,
        0 AS deaths,
        0 AS awarded_kills
    FROM player_killed pk
    WHERE pk.server_id IS NOT NULL
      AND pk.attacker_id IS NOT NULL
      AND pk.victim_id IS NOT NULL
      AND pk.attacker_id <> pk.victim_id

    UNION ALL

    SELECT
        (pk.created_at AT TIME ZONE 'Asia/Shanghai')::date,
        pk.server_id,
        pk.victim_id,
        COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown'),
        'unknown',
        0,
        1,
        0
    FROM player_killed pk
    WHERE pk.server_id IS NOT NULL
      AND pk.attacker_id IS NOT NULL
      AND pk.victim_id IS NOT NULL
      AND pk.attacker_id <> pk.victim_id

    UNION ALL

    SELECT
        (pk.created_at AT TIME ZONE 'Asia/Shanghai')::date,
        pk.server_id,
        pk.awarded_to_id,
        COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown'),
        'unknown',
        0,
        0,
        1
    FROM player_killed pk
    WHERE pk.server_id IS NOT NULL
      AND pk.awarded_to_id IS NOT NULL
      AND pk.attacker_id IS NOT NULL
      AND pk.victim_id IS NOT NULL
      AND pk.attacker_id <> pk.victim_id

    UNION ALL

    SELECT
        (COALESCE(m.ended_at, m.started_at, pmws.created_at) AT TIME ZONE 'Asia/Shanghai')::date,
        pmws.server_id,
        pmws.player_id,
        COALESCE(NULLIF(lower(trim(pmws.weapon)), ''), 'unknown'),
        COALESCE(NULLIF(lower(trim(pmws.input_device)), ''), 'unknown'),
        pmws.kills,
        0,
        0
    FROM player_match_weapon_stats pmws
    JOIN matches m ON m.id = pmws.match_id
    WHERE pmws.server_id IS NOT NULL
      AND pmws.player_id IS NOT NULL
      AND pmws.opponent_id IS NOT NULL
      AND pmws.player_id <> pmws.opponent_id
      AND pmws.kills > 0
      AND NOT EXISTS (
          SELECT 1
          FROM player_killed pk
          WHERE pk.match_id = pmws.match_id
            AND pk.attacker_id = pmws.player_id
            AND pk.victim_id = pmws.opponent_id
            AND COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') = COALESCE(NULLIF(lower(trim(pmws.weapon)), ''), 'unknown')
      )

    UNION ALL

    SELECT
        (COALESCE(m.ended_at, m.started_at, pmws.created_at) AT TIME ZONE 'Asia/Shanghai')::date,
        pmws.server_id,
        pmws.opponent_id,
        COALESCE(NULLIF(lower(trim(pmws.weapon)), ''), 'unknown'),
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
        ),
        0,
        pmws.kills,
        0
    FROM player_match_weapon_stats pmws
    JOIN matches m ON m.id = pmws.match_id
    WHERE pmws.server_id IS NOT NULL
      AND pmws.player_id IS NOT NULL
      AND pmws.opponent_id IS NOT NULL
      AND pmws.player_id <> pmws.opponent_id
      AND pmws.kills > 0
      AND NOT EXISTS (
          SELECT 1
          FROM player_killed pk
          WHERE pk.match_id = pmws.match_id
            AND pk.attacker_id = pmws.player_id
            AND pk.victim_id = pmws.opponent_id
            AND COALESCE(NULLIF(lower(trim(pk.weapon)), ''), 'unknown') = COALESCE(NULLIF(lower(trim(pmws.weapon)), ''), 'unknown')
      )

    UNION ALL

    SELECT
        (COALESCE(m.ended_at, m.started_at, pmws.created_at) AT TIME ZONE 'Asia/Shanghai')::date,
        pmws.server_id,
        pmws.player_id,
        COALESCE(NULLIF(lower(trim(pmws.weapon)), ''), 'unknown'),
        COALESCE(NULLIF(lower(trim(pmws.input_device)), ''), 'unknown'),
        pmws.kills,
        0,
        0
    FROM player_match_weapon_stats pmws
    JOIN matches m ON m.id = pmws.match_id
    WHERE pmws.server_id IS NOT NULL
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
          WHERE pk.match_id = pmws.match_id
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
    SUM(kills)::integer,
    SUM(deaths)::integer,
    SUM(awarded_kills)::integer,
    now()
FROM events
GROUP BY stat_date, server_id, player_id, weapon, input_device;
```

回填对手表：

```sql
WITH events AS (
    SELECT
        (pk.created_at AT TIME ZONE 'Asia/Shanghai')::date AS stat_date,
        pk.server_id,
        pk.attacker_id AS player_id,
        pk.victim_id AS opponent_id,
        1 AS kills,
        0 AS deaths
    FROM player_killed pk
    WHERE pk.server_id IS NOT NULL
      AND pk.attacker_id IS NOT NULL
      AND pk.victim_id IS NOT NULL
      AND pk.attacker_id <> pk.victim_id

    UNION ALL

    SELECT
        (pk.created_at AT TIME ZONE 'Asia/Shanghai')::date,
        pk.server_id,
        pk.victim_id,
        pk.attacker_id,
        0,
        1
    FROM player_killed pk
    WHERE pk.server_id IS NOT NULL
      AND pk.attacker_id IS NOT NULL
      AND pk.victim_id IS NOT NULL
      AND pk.attacker_id <> pk.victim_id

    UNION ALL

    SELECT
        (COALESCE(m.ended_at, m.started_at, pmws.created_at) AT TIME ZONE 'Asia/Shanghai')::date,
        pmws.server_id,
        pmws.player_id,
        pmws.opponent_id,
        pmws.kills,
        0
    FROM player_match_weapon_stats pmws
    JOIN matches m ON m.id = pmws.match_id
    WHERE pmws.server_id IS NOT NULL
      AND pmws.player_id IS NOT NULL
      AND pmws.opponent_id IS NOT NULL
      AND pmws.player_id <> pmws.opponent_id
      AND pmws.kills > 0
      AND NOT EXISTS (
          SELECT 1
          FROM player_killed pk
          WHERE pk.match_id = pmws.match_id
            AND pk.attacker_id = pmws.player_id
            AND pk.victim_id = pmws.opponent_id
      )

    UNION ALL

    SELECT
        (COALESCE(m.ended_at, m.started_at, pmws.created_at) AT TIME ZONE 'Asia/Shanghai')::date,
        pmws.server_id,
        pmws.opponent_id,
        pmws.player_id,
        0,
        pmws.kills
    FROM player_match_weapon_stats pmws
    JOIN matches m ON m.id = pmws.match_id
    WHERE pmws.server_id IS NOT NULL
      AND pmws.player_id IS NOT NULL
      AND pmws.opponent_id IS NOT NULL
      AND pmws.player_id <> pmws.opponent_id
      AND pmws.kills > 0
      AND NOT EXISTS (
          SELECT 1
          FROM player_killed pk
          WHERE pk.match_id = pmws.match_id
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
    SUM(kills)::integer,
    SUM(deaths)::integer,
    now()
FROM events
GROUP BY stat_date, server_id, player_id, opponent_id;

ANALYZE player_kill_daily_weapon_stats;
ANALYZE player_kill_daily_opponent_stats;
```

确认不再需要回滚或审计后，可选删除旧大表释放空间：

```sql
DROP TABLE player_kill_daily_weapon_opponent_stats;
```

快速看估算行数，避免直接 `COUNT(*)` 扫大表：

```sql
SELECT
    relname,
    reltuples::bigint AS estimated_rows
FROM pg_class
WHERE relname IN (
    'player_kill_daily_weapon_stats',
    'player_kill_daily_opponent_stats',
    'player_kill_daily_weapon_opponent_stats'
);
```
