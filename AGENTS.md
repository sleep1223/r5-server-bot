# Repository Guidelines

## Project Structure & Module Organization
This repository is a Python monorepo managed with `uv`.

- `packages/shared_lib/`: shared config, database models, migrations, and reusable utilities.
- `services/ws_service/`: LiveAPI WebSocket ingest service.
- `services/fastapi_service/`: HTTP API service and scheduled sync tasks.
- `services/nonebot_service/`: NoneBot plugin service (`src/plugins/r5`).
- `env/.env.example` and `services/nonebot_service/.env.example`: environment templates.

Keep cross-service logic in `shared_lib`; keep service-specific orchestration inside each service package.

## Build, Test, and Development Commands
- `uv sync --all-packages`: install/update workspace dependencies.
- `uv run python -m ws_service.main`: run the WS service.
- `uv run python -m fastapi_service.main`: run the FastAPI service.
- `cd services/nonebot_service && uv sync && nb run`: run the NoneBot service.
- `uv run ruff check .`: lint from repo root (uses `ruff.toml`).
- `uv run ruff format .`: format code from repo root.
- `cd services/nonebot_service && uv run basedpyright`: type-check NoneBot plugin code.

## Coding Style & Naming Conventions
- Use 4-space indentation and type hints for new/changed Python code.
- Naming: modules/functions/variables in `snake_case`, classes in `PascalCase`, constants in `UPPER_SNAKE_CASE`.
- Imports must be sorted (`ruff` rule `I` is enabled).
- Line length defaults differ:
  - Root workspace: `200` (`ruff.toml`).
  - `services/nonebot_service`: `88` (service-local Ruff config).

## Testing Guidelines
There is currently no committed first-party automated test suite or coverage gate. For new logic:

- Add `pytest` tests under `packages/shared_lib/tests/` or `services/<service>/tests/`.
- Use `test_*.py` naming.
- Prefer fast unit tests for parsers, API clients, and DB query helpers.
- If tests are added, run `uv run pytest` from the repo root.

## Commit & Pull Request Guidelines
Follow the commit pattern already used in history:

- Format: `type(scope): summary` (scope optional).
- Common types: `feat`, `fix`, `refactor`, `docs`.
- Examples: `fix(netcon): ...`, `feat(api): ...`.

For PRs, include:

- concise problem/solution summary,
- impacted modules/services,
- config or migration changes (`.env`, Aerich, schema),
- manual verification steps (commands and expected result),
- API request/response examples when endpoints change.

## Security & Configuration Tips
- Never commit real secrets; keep values in `.env` files and only commit `*.env.example`.
- Review `.gitignore` before adding generated data (SQLite, logs, archives).
- Treat `services/fastapi_service/data/qqwry.dat` and other large data files as runtime assets, not ad-hoc dumps.
