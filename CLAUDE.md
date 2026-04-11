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
- Bot API routes under `/v1/r5/` — endpoints for server info, player queries, KD stats, weapon stats, bans/kicks, donations.
- **Pylon master-server routes under `/v1/`** (no `/r5` prefix) — implement the
  R5 SDK master-server protocol so this service can act as the auth backend
  for game clients/servers. See *Pylon / Steam Authentication* below.

## Pylon / Steam Authentication

The bot doubles as a minimal R5 master server that hands out RS256 JWTs after
verifying a Steam `AuthSessionTicket`. Game servers verify the JWT locally
against the public key served by `/server/auth/keyinfo`. Two layers:

- `services/fastapi_service/src/fastapi_service/services/`
  - `steam_auth_service.py` — calls Steam Web API
    `ISteamUserAuth/AuthenticateUserTicket/v1` (with one auto-retry on
    "Invalid ticket") and optionally fetches the persona name.
  - `jwt_auth_service.py` — RS256 signing + public-key distribution; computes
    `sessionId = sha256(f"{userId}-{playerName}-{serverEndpoint}")` so the
    game server's `CClient::Authenticate` validation matches.
  - `pylon_db_service.py` — `SteamAuthLog` audit writer + ban lookup against
    the existing `Player` / `BanRecord` tables (currently keyed off
    `Player.nucleus_id`; Steam-only users will simply miss until a future
    `Player.steam_id` field is added).
- `services/fastapi_service/src/fastapi_service/api/v1/pylon.py` — exposes:
  - `POST /v1/client/auth`           — Steam ticket → JWT (canonical path)
  - `POST /v1/client/authenticate`   — legacy r5r_sdk alias (accepts `id` as
    a JSON number too)
  - `POST /v1/server/auth/keyinfo`   — JWT public-key distribution; honours
    `keyHash` for the no-change short-circuit at `pylon.cpp:504`
  - `POST /v1/banlist/isBanned`      — single-player ban check used per
    connect by r5r_sdk dedis
  - `POST /v1/banlist/bulkCheck`     — periodic bulk ban check
  - `POST /v1/eula`                  — EULA shim; clients gate every other
    pylon call on this passing
  - `POST /v1/servers/add`           — dedicated-server keep-alive shim
    (echoes `ip`/`port`; optional `token` for hidden servers)

After the reverse proxy strips `/api`, these endpoints live at
`https://r5.sleep0.de/api/v1/...`.

### Required configuration

In `env/.env`:

```ini
steam_web_api_key="<get one at https://steamcommunity.com/dev/apikey>"
steam_app_id="480"  # use the real app id in production
jwt_private_key_path="services/fastapi_service/data/jwt_private.pem"
jwt_public_key_path="services/fastapi_service/data/jwt_public.pem"
jwt_token_ttl_seconds=30
pylon_default_server_port=37015
```

Generate the JWT keypair once on the deploy host:

```bash
openssl genpkey -algorithm RSA -out services/fastapi_service/data/jwt_private.pem -pkeyopt rsa_keygen_bits:2048
openssl rsa -in services/fastapi_service/data/jwt_private.pem -pubout -out services/fastapi_service/data/jwt_public.pem
chmod 600 services/fastapi_service/data/jwt_private.pem
```

The keypair powers both `/server/auth/keyinfo` (returns the public key,
base64-encoded, with `keyHash = sha256(pem)`) and `/client/auth` (signs
short-lived JWTs with the private key). Rotate by replacing both files; the
service hot-reloads on file mtime change.

### Database

`SteamAuthLog` (table `steam_auth_log`) is added by migration
`packages/shared_lib/migrations/models/10_20260411150000_steam_auth_log.py`.
On first boot `Tortoise.generate_schemas()` will create the table for fresh
DBs; existing prod DBs need `aerich upgrade` (or running the migration's
SQL manually).

### Compatibility matrix

- **r5v_sdk** (Steam-native, hardcoded JWT public key in
  `engine/client/client.cpp:81`) — use `/v1/client/auth`. The SDK's hardcoded
  public key must match the one served at `/v1/server/auth/keyinfo`, so
  either align keys with r5v upstream or recompile r5v_sdk with your own.
- **r5r_sdk** (Origin legacy, fetches public key at runtime) — call
  `/v1/client/authenticate` and `/v1/server/auth/keyinfo`. Requires the
  per-client Steam plugin DLL described in the project's docs to actually
  send a Steam ticket; otherwise this endpoint is unused.

## NoneBot Service Details

- Runs independently from the main workspace — `cd services/nonebot_service` before any uv/nb commands.
- Communicates with FastAPI service via `R5ApiClient` (HTTP client in `api_client.py`).
- Plugin services in `src/plugins/r5/services/`: admin, donation, help, kd, query, status, weapons.
