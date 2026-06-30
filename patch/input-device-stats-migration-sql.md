# 输入设备统计迁移 SQL

> 已废弃：当前代码已改为 `player_kill_daily_weapon_stats` + `player_kill_daily_opponent_stats` 拆表方案，不要再执行本文中的旧表迁移。请使用 `patch/collapse-daily-stats-opponent-dimension-sql.md`。

用于把历史统计数据的输入设备统一标记为 `unknown`，并为 SDK 对局结束上报产生的新武器统计启用输入设备维度。

```sql
BEGIN;

ALTER TABLE players
    ADD COLUMN IF NOT EXISTS input_device varchar(50);

UPDATE players
SET input_device = 'unknown'
WHERE input_device IS NULL OR btrim(input_device) = '';

ALTER TABLE player_match_weapon_stats
    ADD COLUMN IF NOT EXISTS input_device varchar(50) NOT NULL DEFAULT 'unknown';

UPDATE player_match_weapon_stats
SET input_device = 'unknown'
WHERE input_device IS NULL OR btrim(input_device) = '';

CREATE INDEX IF NOT EXISTS idx_pmws_input_device_weapon
    ON player_match_weapon_stats (input_device, weapon);

ALTER TABLE player_kill_daily_weapon_opponent_stats
    ADD COLUMN IF NOT EXISTS input_device varchar(50) NOT NULL DEFAULT 'unknown';

UPDATE player_kill_daily_weapon_opponent_stats
SET input_device = 'unknown'
WHERE input_device IS NULL OR btrim(input_device) = '';

DO $$
DECLARE
    constraint_name text;
BEGIN
    SELECT tc.constraint_name
    INTO constraint_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_schema = tc.constraint_schema
     AND kcu.constraint_name = tc.constraint_name
     AND kcu.table_name = tc.table_name
    WHERE tc.table_schema = 'public'
      AND tc.table_name = 'player_kill_daily_weapon_opponent_stats'
      AND tc.constraint_type = 'UNIQUE'
    GROUP BY tc.constraint_name
    HAVING array_agg(kcu.column_name ORDER BY kcu.ordinal_position)
        = ARRAY['stat_date', 'server_id', 'player_id', 'opponent_id', 'weapon'];

    IF constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE player_kill_daily_weapon_opponent_stats DROP CONSTRAINT %I',
            constraint_name
        );
    END IF;
END $$;

DO $$
DECLARE
    constraint_name text;
BEGIN
    SELECT tc.constraint_name
    INTO constraint_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_schema = tc.constraint_schema
     AND kcu.constraint_name = tc.constraint_name
     AND kcu.table_name = tc.table_name
    WHERE tc.table_schema = 'public'
      AND tc.table_name = 'player_kill_daily_weapon_opponent_stats'
      AND tc.constraint_type = 'UNIQUE'
    GROUP BY tc.constraint_name
    HAVING array_agg(kcu.column_name ORDER BY kcu.ordinal_position)
        = ARRAY['stat_date', 'server_id', 'player_id', 'opponent_id', 'weapon', 'input_device'];

    IF constraint_name IS NULL THEN
        ALTER TABLE player_kill_daily_weapon_opponent_stats
            ADD CONSTRAINT player_kill_daily_weapon_opponent_stats_input_device_unique
            UNIQUE (stat_date, server_id, player_id, opponent_id, weapon, input_device);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_pkdwos_stat_date_input_device_kills
    ON player_kill_daily_weapon_opponent_stats (stat_date, input_device, kills);

COMMIT;
```

迁移后建议刷新近期日统计窗口，让 `player_kill_daily_weapon_opponent_stats` 重新从 `player_match_weapon_stats.input_device` 聚合：

```sql
-- 也可以等待定时任务自动刷新 lookback 窗口。
-- 如果需要更长历史范围，请临时调大 player_kill_daily_stats_refresh_lookback_days 后启动服务。
```
