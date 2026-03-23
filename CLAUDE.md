# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

R5-Server-Bot is a Python monorepo for managing R5 Reloaded (Apex Legends) game servers. It ingests LiveAPI events via WebSocket, exposes an HTTP API for server/player data, and provides a chat bot interface (QQ/Kaiheila) for player queries and admin commands.

## Common Commands

```bash
# Install all workspace dependencies
uv sync --all-packages

# Run services (each in a separate terminal)
uv run python -m ws_service.main        # WebSocket ingest service
uv run python -m fastapi_service.main   # FastAPI HTTP API service
cd services/nonebot_service && uv sync && nb run  # NoneBot chat bot

# Code quality
uv run ruff check .       # Lint
uv run ruff format .      # Format
cd services/nonebot_service && uv run basedpyright  # Type-check NoneBot code

# Tests (no test suite committed yet; if added)
uv run pytest
```

## Architecture

Three services share a common library through a `uv` workspace:

```
shared_lib (packages/shared_lib)
  ├── Config (pydantic-settings, loads from env/.env)
  ├── Database (Tortoise ORM async, SQLite dev / PostgreSQL prod)
  ├── Models: Player, events (PlayerKilled, PlayerConnected, etc.), IpInfo, Donation, BanRecord
  └── Utilities: IP geolocation (qqwry), KD leaderboard calculations, RCON netcon client, protobuf defs

ws_service → WebSocket server receiving LiveAPI protobuf events, persisting to DB
fastapi_service → HTTP API + background tasks (server list polling, IP resolution, player sync)
nonebot_service → NoneBot 2 chat bot plugins calling fastapi_service over HTTP
```

**Key data flow:** LiveAPI events → ws_service → DB ← fastapi_service (API + background tasks) ← nonebot_service (chat commands)

## Workspace Layout

- **`packages/shared_lib/`** — shared config, DB models, migrations (Aerich), utilities. Cross-service logic goes here.
- **`services/ws_service/`** — WebSocket LiveAPI listener. Entry: `ws_service.main`.
- **`services/fastapi_service/`** — FastAPI app with bearer-token auth, caches, background tasks. Entry: `fastapi_service.main`.
- **`services/nonebot_service/`** — **Separate from the uv workspace** (has its own `pyproject.toml` and `uv.lock`). Uses NoneBot 2 with OneBot and Kaiheila adapters.

## Important Conventions

- **Line length:** 200 (root workspace via `ruff.toml`), 88 (`nonebot_service` local config).
- **Naming:** `snake_case` for modules/functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- **Type hints** required for new/changed code.
- **Imports** must be sorted (ruff rule `I`).
- **Protobuf files** (`*_pb2.py`, `*_pb2.pyi`) are generated — excluded from linting.
- **Commits:** `type(scope): summary` — types: `feat`, `fix`, `refactor`, `docs`.
- **Secrets:** Never commit `.env` files; only `*.env.example` templates.

## Database & Migrations

- ORM: Tortoise ORM (async) with asyncpg for PostgreSQL.
- Migrations: Aerich, configured in root `pyproject.toml` (`tortoise_orm = "shared_lib.database.TORTOISE_ORM"`).
- Migration files: `packages/shared_lib/migrations/`.
- Settings class in `packages/shared_lib/src/shared_lib/config.py` — loads from `env/.env`.

## FastAPI Service Details

- Authentication: `HTTPBearer` with token list from `settings.fastapi_access_tokens`.
- Background tasks started in lifespan: server list fetch (5s interval), IP resolution, player sync.
- Caches in `api/v1/r5/cache.py`: `global_server_cache`, `raw_server_response_cache`, `banned_player_server_cache`.
- API routes under `/v1/r5/` — endpoints for server info, player queries, KD stats, weapon stats, bans/kicks, donations.

## NoneBot Service Details

- Runs independently from the main workspace — `cd services/nonebot_service` before any uv/nb commands.
- Communicates with FastAPI service via `R5ApiClient` (HTTP client in `api_client.py`).
- Plugin services in `src/plugins/r5/services/`: admin, donation, help, kd, query, status, weapons.
