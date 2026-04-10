#!/bin/bash
zip -r r5-server-bot.zip \
  packages/shared_lib \
  services/fastapi_service \
  services/nonebot_service \
  services/ws_service \
  pyproject.toml \
  -x "*__pycache__*" \
  -x "*.py[cod]" \
  -x "*\$py.class" \
  -x "*/.env" \
  -x "*/.env.*" \
  -x "*.sqlite3" \
  -x "*.db" \
  -x "**/migrations/*" \
  -x "*/.venv/*" \
  -x "*/.idea/*" \
  -x "*.log" \
  -x "*/logs/*" \
  -x "*/build/*" \
  -x "*/dist/*" \
  -x "*.egg-info/*" \
  -x "*/.ruff_cache/*" \
  -x "*/.git/*" \
  -x ".ruff_cache/*" \
  -x "*/data/*" \
  -x "*.lock" \
  -x "*_pb2.py" \
  -x "*_pb2.pyi" \
  -x "*.proto"
