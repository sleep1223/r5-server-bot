#!/bin/bash
set -e

MODE="all"
if [ "$1" = "ws" ] || [ "$1" = "--ws" ]; then
  MODE="ws"
fi

EXCLUDES=(
  -x "*__pycache__*"
  -x "*.py[cod]"
  -x "*\$py.class"
  -x "*/.env"
  -x "*/.env.*"
  -x "*.sqlite3"
  -x "*.db"
  -x "**/migrations/*"
  -x "*/.venv/*"
  -x "*/.idea/*"
  -x "*.log"
  -x "*/logs/*"
  -x "*/build/*"
  -x "*/dist/*"
  -x "*.egg-info/*"
  -x "*/.ruff_cache/*"
  -x "*/.git/*"
  -x ".ruff_cache/*"
  -x "*/data/*"
  -x "*.lock"
)

if [ "$MODE" = "ws" ]; then
  zip -r r5-server-bot-ws.zip \
    packages/shared_lib \
    services/ws_service \
    pyproject.toml \
    uv.toml \
    "${EXCLUDES[@]}"
else
  zip -r r5-server-bot.zip \
    packages/shared_lib \
    services/fastapi_service \
    services/nonebot_service \
    services/ws_service \
    pyproject.toml \
    uv.toml \
    "${EXCLUDES[@]}"
fi
