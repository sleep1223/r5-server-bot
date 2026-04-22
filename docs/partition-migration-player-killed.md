# `player_killed` 分区表迁移手册

把 `player_killed` 从普通表改造为 PG 原生**按月份分区**的表（`RANGE (created_at)` + `pg_partman v5` 自动维护），在线、零停机、可回退。

## 背景与目标

- **表**：`public.player_killed`（击杀事件，LiveAPI ingest 高频写入）
- **痛点**：单表 heap + 单 PK 索引，行数大了之后 vacuum/autoanalyze 代价高；按时间窗口查询无法剪枝；历史数据清理只能 `DELETE` + bloat。
- **目标结构**
  - 按 `created_at` **月度分区**（`RANGE` 分区）
  - 复合主键 `(id, created_at)`（分区表要求分区键必须在唯一约束里）
  - 由 `pg_partman v5` 自动预建未来分区 + 可选 retention 清理
- **不停服原则**：全程靠"新表 + 双写触发器 + 分段回填 + 秒级改名"完成切换。

## 前提 / 环境要求

- PostgreSQL ≥ 13（分区表功能足够成熟；PG 14+ 更优）
- `pg_partman` ≥ 5（v5 起父表索引/FK/CHECK 自动下发子分区，**无需模板表**；v5 移除了后台 worker，需要外部调度）
- 有能力在 DB 上跑 DDL 的角色（`CREATE`/`ALTER`/`pg_cron` 权限）
- **代码侧**：所有写入走 Tortoise `bulk_create`（见 `_dispatch_event` → `queue` → `model_cls.bulk_create`），没有 `UPDATE`/`DELETE player_killed`，没有别的表用 FK 指向 `player_killed`。如果引入了，走下面的"代码侧前置检查"。

---

## Step 0 — 前置检查

### 0.1 DB 侧

```sql
-- 版本 & schema 确认
SELECT extname, extversion, extnamespace::regnamespace AS schema
FROM pg_extension WHERE extname = 'pg_partman';

-- 数据时间范围（决定 create_parent 里 p_start_partition 的起始月）
SELECT min(created_at), max(created_at), count(*) FROM public.player_killed;

-- 有没有别的表外键指向 player_killed
SELECT conrelid::regclass AS from_table, conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE confrelid = 'public.player_killed'::regclass AND contype = 'f';

-- 列清单（后面双写触发器 UPDATE 分支要按列展开）
\d+ public.player_killed
```

> 如果 0.1 最后一条出了其他表的 FK，这些 FK 在切换时不能直接指向分区表（PG 不允许 FK 引用分区父表的非主键列，但引用 `(id, created_at)` 又需要两列）。方案：把那些 FK 改成 `db_constraint=False`（Tortoise 侧 `ForeignKeyField("models.PlayerKilled", db_constraint=False)`），靠应用层保一致。

### 0.2 代码侧

```bash
# 1) bulk_create on_conflict 必须带上 created_at
grep -rn "PlayerKilled.*bulk_create\|on_conflict" --include="*.py"

# 2) 其他模型是否 FK 到 PlayerKilled（目前仅作为 related_name 的反向关系，没有正向 FK）
grep -rn 'ForeignKeyField.*"models.PlayerKilled"' --include="*.py"
```

- 若 `bulk_create(on_conflict=["id"], ...)` 出现过，必须改成 `on_conflict=["id", "created_at"]`（分区表唯一约束是复合的）。
- 若有 `ForeignKeyField("models.PlayerKilled", ...)` 出现过，加 `db_constraint=False`，并在 Aerich 里手动写 DROP CONSTRAINT。

> 本仓库当前状态：`PlayerKilled.bulk_create` 无 `on_conflict`；没有正向 FK 指向它。**无需改 ORM。**

---

## Step 1 — 建分区父表

### 1.1 父表骨架

```sql
CREATE TABLE public.player_killed_new (
    LIKE public.player_killed INCLUDING DEFAULTS INCLUDING IDENTITY
) PARTITION BY RANGE (created_at);

-- 复合主键（分区键必须在 UNIQUE / PRIMARY KEY 里）
ALTER TABLE public.player_killed_new
    ADD PRIMARY KEY (id, created_at);
```

`INCLUDING DEFAULTS INCLUDING IDENTITY` 让新父表继承 `id` 的 serial/identity 定义，复用同一个 sequence，Step 7 改名后新表直接续写 id 不跳号。

### 1.2 外键（建在父表，自动下发所有子分区）

```sql
ALTER TABLE public.player_killed_new
    ADD CONSTRAINT fk_pk_attacker FOREIGN KEY (attacker_id)   REFERENCES public.players(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_pk_victim   FOREIGN KEY (victim_id)     REFERENCES public.players(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_pk_awarded  FOREIGN KEY (awarded_to_id) REFERENCES public.players(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_pk_server   FOREIGN KEY (server_id)     REFERENCES public.servers(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_pk_match    FOREIGN KEY (match_id)      REFERENCES public.matches(id)  ON DELETE SET NULL;
```

### 1.3 索引

```sql
CREATE INDEX idx_pk_attacker   ON public.player_killed_new (attacker_id);
CREATE INDEX idx_pk_victim     ON public.player_killed_new (victim_id);
CREATE INDEX idx_pk_server     ON public.player_killed_new (server_id);
CREATE INDEX idx_pk_match      ON public.player_killed_new (match_id);
CREATE INDEX idx_pk_weapon     ON public.player_killed_new (weapon);
CREATE INDEX idx_pk_created_at ON public.player_killed_new (created_at);
CREATE INDEX idx_pk_id         ON public.player_killed_new (id);  -- 兜底按 id 点查
```

> v5 下父表的索引 / FK / CHECK 都会自动下发到新分区，不需要模板表，`create_parent` 里也不传 `p_template_table`。

---

## Step 2 — 注册 pg_partman

把 `p_start_partition` 改成 Step 0 里 `min(created_at)` 所在那个月的 1 号：

```sql
SELECT partman.create_parent(
      p_parent_table    := 'public.player_killed_new'
    , p_control         := 'created_at'
    , p_interval        := '1 month'
    , p_type            := 'range'
    , p_premake         := 6
    , p_start_partition := '2026-01-01'
);

-- 无限预建未来分区（不会因为没人调维护就写穿 default 分区）
UPDATE partman.part_config
SET infinite_time_partitions = true
WHERE parent_table = 'public.player_killed_new';
```

**先不开 retention**，数据搬完再开（Step 9）。

验证：

```sql
SELECT parent_table, control, partition_interval, premake, infinite_time_partitions
FROM partman.part_config
WHERE parent_table = 'public.player_killed_new';

-- 确认子分区真的建出来了（应该能看到从 p_start_partition 起 premake 个月的分区）
\d+ public.player_killed_new
```

---

## Step 3 — 维护调度

v5 移除了后台 worker，**必须自己调度**。二选一：

### 方案 A — pg_cron（推荐，DB 内置）

```sql
CREATE EXTENSION IF NOT EXISTS pg_cron;

SELECT cron.schedule(
    'partman-maint',
    '0 * * * *',
    $$CALL partman.run_maintenance_proc()$$
);
```

### 方案 B — 系统 cron

```cron
0 * * * *  psql -d yourdb -c "CALL partman.run_maintenance_proc();"
```

**立刻跑一次做一次烟测**：

```sql
CALL partman.run_maintenance_proc();
```

---

## Step 4 — 双写触发器

目的：从此刻起，所有对 `player_killed` 的 `INSERT`/`UPDATE`/`DELETE` 都同步到 `player_killed_new`，保证 Step 5 的回填能和实时写入会合。

```sql
CREATE OR REPLACE FUNCTION public.player_killed_dualwrite() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO public.player_killed_new VALUES (NEW.*)
        ON CONFLICT (id, created_at) DO NOTHING;
        RETURN NEW;

    ELSIF TG_OP = 'UPDATE' THEN
        INSERT INTO public.player_killed_new VALUES (NEW.*)
        ON CONFLICT (id, created_at) DO UPDATE
           SET "timestamp"     = EXCLUDED."timestamp",
               attacker_id     = EXCLUDED.attacker_id,
               victim_id       = EXCLUDED.victim_id,
               awarded_to_id   = EXCLUDED.awarded_to_id,
               attacker_data   = EXCLUDED.attacker_data,
               victim_data     = EXCLUDED.victim_data,
               awarded_to_data = EXCLUDED.awarded_to_data,
               weapon          = EXCLUDED.weapon,
               server_id       = EXCLUDED.server_id,
               match_id        = EXCLUDED.match_id;
        RETURN NEW;

    ELSIF TG_OP = 'DELETE' THEN
        DELETE FROM public.player_killed_new
         WHERE id = OLD.id AND created_at = OLD.created_at;
        RETURN OLD;
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER player_killed_dualwrite_trg
AFTER INSERT OR UPDATE OR DELETE ON public.player_killed
FOR EACH ROW EXECUTE FUNCTION public.player_killed_dualwrite();
```

> UPDATE 分支里的列清单**必须按 Step 0 `\d+ public.player_killed` 的实际列**维护。将来给 `PlayerKilled` 加字段，要同时改这里 **和** `CREATE TABLE ... LIKE` 的新表结构（实际上 `LIKE ... INCLUDING DEFAULTS` 在这一步之前就做完了，所以：加字段的节奏是"老表加 → 新表加 → 重建触发器 → 再 rollout 代码"）。

验证双写：

```sql
-- 让最新一条老记录自我 UPDATE 一下，新表应出现同一条
UPDATE public.player_killed SET weapon = weapon
WHERE id = (SELECT max(id) FROM public.player_killed);

SELECT * FROM public.player_killed_new ORDER BY id DESC LIMIT 1;
```

---

## Step 5 — 分段回填历史

触发器已接管当下。把老数据按月搬到新表：

```sql
-- 单月示例
INSERT INTO public.player_killed_new
SELECT * FROM public.player_killed
WHERE created_at >= '2026-01-01' AND created_at < '2026-02-01'
ON CONFLICT (id, created_at) DO NOTHING;
```

- **按月回填**是基本节奏；单月行数过大时切成天。
- `ON CONFLICT DO NOTHING` 保证和触发器双写不撞（触发器可能已经把某条提前写进新表）。
- 过程中业务继续写，**不影响**。

---

## Step 6 — 校验

```sql
-- 行数应相等
SELECT (SELECT count(*) FROM public.player_killed)     AS old_cnt,
       (SELECT count(*) FROM public.player_killed_new) AS new_cnt;

-- 近 1 小时抽样核对：对所有 id 做有序哈希比对
WITH a AS (SELECT md5(string_agg(id::text, ',' ORDER BY id)) h
           FROM public.player_killed     WHERE created_at > now() - interval '1 hour'),
     b AS (SELECT md5(string_agg(id::text, ',' ORDER BY id)) h
           FROM public.player_killed_new WHERE created_at > now() - interval '1 hour')
SELECT a.h = b.h AS matched FROM a, b;

-- 分区裁剪生效：只应扫一个分区
EXPLAIN SELECT * FROM public.player_killed_new
WHERE created_at >= '2026-04-01' AND created_at < '2026-04-02';
```

`EXPLAIN` 期望输出里 `Append` 节点下**只有一个 `Seq Scan`/`Index Scan`**（当月分区）。如果看到所有分区都被扫，检查 Step 1 父表的 `PARTITION BY` 和 Step 2 的 `p_control`。

---

## Step 7 — 切换（秒级锁）

```sql
BEGIN;

LOCK TABLE public.player_killed IN ACCESS EXCLUSIVE MODE;

-- 补触发器可能没接住的缝隙（极短时间窗，保险）
INSERT INTO public.player_killed_new
SELECT * FROM public.player_killed
WHERE created_at >= now() - interval '10 minutes'
ON CONFLICT (id, created_at) DO NOTHING;

-- 拆触发器
DROP TRIGGER player_killed_dualwrite_trg ON public.player_killed;

-- 改名
ALTER TABLE public.player_killed     RENAME TO player_killed_archive;
ALTER TABLE public.player_killed_new RENAME TO player_killed;

-- 同步 part_config 里的表名，维护任务才能找得到
UPDATE partman.part_config
SET parent_table = 'public.player_killed'
WHERE parent_table = 'public.player_killed_new';

-- 序列对齐（老表改名后 id sequence 还挂在同一个物理 sequence 上，setval 后续号从 max(id)+1 开始）
SELECT setval(
    pg_get_serial_sequence('public.player_killed', 'id'),
    (SELECT max(id) FROM public.player_killed)
);

COMMIT;
```

这段事务拿 `ACCESS EXCLUSIVE` 锁，期间所有读写都会阻塞——所以要**尽量短**。补缝隙 INSERT 只扫最近 10 分钟，通常毫秒级；改名是瞬时的 catalog 操作。总锁时长通常 < 1s。

> **ingest 侧表现**：ws_service/fastapi_service 的 `bulk_create` 会 retry 或延迟返回；`IngestBatch` 有 batch_id 去重逻辑，不会造成数据丢失。

---

## Step 8 — 应用层配合

代码侧已在[这次分区改造的 commit](../services/fastapi_service/src/fastapi_service/services/match_service.py) 里处理：

- `PlayerKilled` 模型 docstring 标注分区 + 复合 PK 约束
- 高频查询（`get_recent_matches` / `get_player_matches` / `get_competitive_ranking`）都加了 `created_at` 范围过滤，走分区裁剪
- `_pk_created_at_bounds(matches)` helper 从一批 match 的 `started_at`/`ended_at` 推 `created_at` 上下界，lead 2h / tail 30min 覆盖事件时序抖动

**未覆盖的全时段聚合查询**（`get_player_vs_all` / `get_player_weapon_stats` / `team_service._get_player_kd` / `utils/kd_leaderboard.py`）：语义是历史全量，分区裁剪帮不上，留给未来引入"留存窗口"时统一处理。

---

## Step 9 — 收尾（观察 1–2 天稳定后）

### 删归档

```sql
DROP TABLE public.player_killed_archive;
DROP FUNCTION public.player_killed_dualwrite();
```

### 打开 retention（可选）

```sql
UPDATE partman.part_config
SET retention = '24 months',
    retention_keep_table = true     -- 先只 detach 不 drop
WHERE parent_table = 'public.player_killed';
```

- `retention = '24 months'`：保留最近 24 个月。
- `retention_keep_table = true`：超期分区只从分区树 **detach**、保留表文件（可 `pg_dump` 归档到冷存储后再 `DROP`）。稳妥后改成 `false` 就是到期自动 `DROP`。
- `run_maintenance_proc()` 下次执行时会按 retention 清理。

---

## 回滚剧本

Step 7 改名完成**之前**，回滚很简单：

1. `DROP TRIGGER player_killed_dualwrite_trg ON public.player_killed;`
2. `DROP TABLE public.player_killed_new CASCADE;`
3. `DELETE FROM partman.part_config WHERE parent_table = 'public.player_killed_new';`
4. 老表 `public.player_killed` 丝毫未动。

Step 7 之后（改名完成）回滚：

1. 把 `application` 下线（或只读）。
2. `ALTER TABLE public.player_killed RENAME TO player_killed_partitioned;`
3. `ALTER TABLE public.player_killed_archive RENAME TO player_killed;`
4. `SELECT setval(pg_get_serial_sequence('public.player_killed','id'), (SELECT max(id) FROM public.player_killed));`
5. 在切换窗口内写到新（分区）表、而老（归档）表没收到的数据需要反向回填：
   ```sql
   INSERT INTO public.player_killed
   SELECT * FROM public.player_killed_partitioned
   WHERE created_at > <切换时刻>
   ON CONFLICT DO NOTHING;
   ```
6. 应用上线恢复读写。

所以——**Step 9 删归档表前，千万别 DROP。**

---

## 运维 Cheatsheet

| 场景 | 命令 |
|---|---|
| 看当前所有分区 | `\d+ public.player_killed` |
| 看 part_config | `SELECT * FROM partman.part_config WHERE parent_table='public.player_killed';` |
| 手动跑一次维护 | `CALL partman.run_maintenance_proc();` |
| 立刻预建下个月分区 | 调 `run_maintenance_proc()` 就会按 premake 补 |
| 看 pg_cron 任务 | `SELECT * FROM cron.job;` / `SELECT * FROM cron.job_run_details ORDER BY start_time DESC LIMIT 20;` |
| 单分区查询（确认裁剪） | `EXPLAIN SELECT ... WHERE created_at >= '<month>' AND created_at < '<next-month>';` |
| default 分区应一直为空 | `SELECT count(*) FROM public.player_killed_default;` 若 > 0 说明 premake 不够或调度挂了 |

## 加字段的流程（未来维护）

给 `PlayerKilled` 加字段时按这个顺序做，避免触发器/分区对不上：

1. 老表加字段（上线前）：`ALTER TABLE public.player_killed ADD COLUMN ...;`（如果老表还在用）
2. 父分区表加字段：`ALTER TABLE public.player_killed ADD COLUMN ...;`（切换后就是这一张）
3. 子分区由 PG 自动继承，不需要逐个加
4. 更新 ORM 模型 + Aerich migration
5. 如果还在 Step 4–7 之间，**必须**同步更新 `player_killed_dualwrite` 的 UPDATE 分支列清单
