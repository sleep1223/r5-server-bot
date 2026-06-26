# PlayerMatchWeaponStat 被击杀者迁移 SQL

用于让 SDK 对局结束上报的 `killEvents` 写入 `player_match_weapon_stats.opponent_id`。

```sql
BEGIN;

ALTER TABLE player_match_weapon_stats
    ADD COLUMN IF NOT EXISTS opponent_id integer NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'player_match_weapon_stats_opponent_id_fkey'
    ) THEN
        ALTER TABLE player_match_weapon_stats
            ADD CONSTRAINT player_match_weapon_stats_opponent_id_fkey
            FOREIGN KEY (opponent_id) REFERENCES players(id)
            ON DELETE SET NULL;
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
      AND tc.table_name = 'player_match_weapon_stats'
      AND tc.constraint_type = 'UNIQUE'
    GROUP BY tc.constraint_name
    HAVING array_agg(kcu.column_name ORDER BY kcu.ordinal_position)
        = ARRAY['match_id', 'player_id', 'weapon', 'source'];

    IF constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE player_match_weapon_stats DROP CONSTRAINT %I',
            constraint_name
        );
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_pmws_match_player_opponent_weapon_source
    ON player_match_weapon_stats (
        match_id,
        player_id,
        COALESCE(opponent_id, 0),
        weapon,
        source
    );

CREATE INDEX IF NOT EXISTS idx_pmws_match_player_opponent
    ON player_match_weapon_stats (match_id, player_id, opponent_id);

CREATE INDEX IF NOT EXISTS idx_pmws_opponent_weapon
    ON player_match_weapon_stats (opponent_id, weapon);

COMMIT;
```
