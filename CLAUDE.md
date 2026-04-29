# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

## 项目概览

R5 Reloaded 游戏服务器的运维 / 战绩 bot，是一个 uv 管理的 Python 3.12 monorepo（NoneBot 插件子项目使用 3.10+），由三个独立运行的服务 + 一个共享库组成：

- `services/ws_service` — 监听游戏服务端 LiveAPI WebSocket（`liveapi.cfg`），解析 protobuf 事件，批量上报到 FastAPI 的 ingest 子进程；TTY 下用 rich.Live 渲染 dashboard。
- `services/fastapi_service` — 主 HTTP API（Granian, `workers=1`），路由前缀 `/r5`，包含查询、管理、leaderboard、对局、捐赠、launcher、Steam/Pylon 鉴权等。后台任务：拉取远程服务器列表、RCON 同步玩家、IP 解析、stale match 关闭、match 对账。
- `services/fastapi_service` 的 `ingest_main:app` — **独立 Granian 进程**（端口默认 8010），只挂 `/v1/r5/ingest/*`，靠 `workers=1` 保证去重 LRU 与 batch 锁全局唯一；schema 由主 app 生成，ingest 进程 `init_db(generate_schemas=False)`。
- `services/nonebot_service` — OneBot V11 适配的 QQ 机器人（`uv` workspace 中**已排除**，需独立 `uv sync`）；通过 HTTP 调主 FastAPI；插件目录 `src/plugins/r5/services/{kd,match,query,status,team,weapons,binding,donation,admin,friend,help}.py`。
- `packages/shared_lib` — 跨服务的 `config.Settings`（pydantic-settings，从 `env/.env` 读取）、Tortoise ORM `models.py`、`schemas/ingest.py`（ingest payload 协议）、`utils/netcon_client.py`（RCON）、protobuf 生成的 `utils/protos`。

数据流：游戏服 →(WS/LiveAPI)→ `ws_service` →(HTTP batch + Bearer token)→ `fastapi_service ingest 进程` →(Tortoise ORM)→ DB ←(查询)— `fastapi_service 主进程` ←(httpx)— `nonebot_service` ←(OneBot)— QQ。

## 关键架构约束

- **Granian `workers=1` 不可改**：进程内 `server_cache`、`_ACTIVE_MATCH_BY_SERVER`、ingest 去重 LRU、后台 scheduler 都依赖单进程共享状态，多 worker 会裂脑或重复执行。
- **Ingest 拆独立进程**：主 API 与 ingest 必须分别启动，DDL 只在主 app `init_db()` 中执行，ingest 仅连库。
- **配置中心化**：所有可调参数都在 `shared_lib/config.py` 的 `Settings`，通过 `env/.env` 覆盖；新增配置先加这里，不要在服务里散落硬编码。
- **数据库迁移**：使用 aerich，配置在根 `pyproject.toml` 的 `[tool.aerich]`，迁移文件在 `packages/shared_lib/migrations/models/`；新建迁移：`uv run aerich migrate --name <name>`，应用：`uv run aerich upgrade`。
- **NoneBot 子项目独立依赖图**：根 `pyproject.toml` 的 `[tool.uv.workspace]` 显式 `exclude` 了 `nonebot_service`，因为 NoneBot 生态需要 py3.10+ 与不同的依赖；改完根包后若 nonebot 端要更新还需 `cd services/nonebot_service && uv sync`。
- **统计排除规则**：无规则 / 纯娱乐服通过 `no_cover_allowed_server_*` 与 `kd_excluded_server_hosts` 配置过滤，写战绩 / 风控逻辑时记得先查 settings。
- **proto / qqwry / jwt key 等数据文件**：路径都来自 `Settings`（如 `qqwry_path`、`jwt_private_key_path`），不要写死。

## 常用命令

依赖管理（所有命令在仓库根执行，除 nonebot 外）：
```shell
uv sync --all-packages          # 安装 workspace 全部包
cd services/nonebot_service && uv sync   # nonebot 子项目独立同步
```

启动三个服务（分别在三个终端）：
```shell
uv run python -m ws_service.main                  # WS 监听
uv run python -m fastapi_service.server           # 主 API（Granian 启动 fastapi_service.main:app，仅主路由）
uv run python -m fastapi_service.ingest_server    # ingest 独立进程（Granian 启动 fastapi_service.ingest_main:app，必须单独启动）
cd services/nonebot_service && nb run             # NoneBot
```

数据库迁移（aerich）：
```shell
uv run aerich migrate --name <desc>
uv run aerich upgrade
uv run aerich history
```

代码质量：
```shell
uv run ruff check .             # lint（根 ruff.toml: E/F/I, line-length=200, F401 unfixable）
uv run ruff format .            # 格式化
uv run basedpyright             # 类型检查（standard 模式，nonebot 子项目排除）
```
nonebot 子项目自带更严格的 ruff 配置（pyproject `[tool.ruff]`），改 `services/nonebot_service` 时在该目录下跑 `uv run ruff check .`。

打包发布：
```shell
./pack.sh         # 全量打包到 r5-server-bot.zip
./pack.sh ws      # 仅打包 ws_service + shared_lib
```

## 编辑提示

- 改 ingest 协议 → 同步 `packages/shared_lib/src/shared_lib/schemas/ingest.py` 与 `services/fastapi_service/.../api/v1/ingest.py` 两端。
- 新增 FastAPI 路由 → 在 `api/v1/` 下加文件，并在 `api/v1/router.py` 用 `include_router` 注册（注意 ingest 路由不在主 router 里，挂在 `ingest_main`）。
- 新增 NoneBot 指令 → 在 `services/nonebot_service/src/plugins/r5/services/` 下加模块，并在 `plugins/r5/__init__.py` 末尾的 `from .services import ...` 与 `__all__` 中加上；通过 `api_client.py` 调主 API。
- 新增后台任务 → 在 `fastapi_service/tasks/` 下加，并在 `tasks/scheduler.py` 中注册；周期参数走 `Settings`。
- 修改模型 → 编辑 `shared_lib/models.py` 后必须 `aerich migrate`。
