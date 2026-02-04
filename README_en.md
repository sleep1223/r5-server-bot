# R5-bot Deployment Guide

[中文版本](README.md)

This guide introduces how to configure the environment and start the various service components of R5-bot.

## Environment Preparation

### 1. Configure pip Mirror (Optional)
To accelerate the download of dependency packages, it is recommended to configure a domestic mirror source:
```shell
pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
```

### 2. Install uv
This project uses `uv` for dependency management and execution:
```shell
pip install uv
```
*Note: After installation, please restart the terminal to ensure the `uv` command takes effect.*

### 3. Install nb-cli
It is recommended to install the `nb-cli` tool for managing NoneBot:
```shell
uv tool install nb-cli
```

## Configuration

### 1. Environment Variables
Copy the `env/.env.example` file to `env/.env` and modify the configuration as needed.

**Key settings in `env/.env`:**
```properties
# Console output netkey (corresponds to liveapi.cfg)
r5_target_keys='["server_key"]'

# RCON configuration (corresponds to rcon_server.cfg)
r5_rcon_key="rcon_key"
r5_rcon_password="sv_rcon_password"

# WS Service settings
ws_host="127.0.0.1"
ws_port=7771

# FastAPI Service settings
fastapi_host="0.0.0.0"
fastapi_port=8000
fastapi_access_tokens='["your_api_token"]'
```

### 2. Game Server Configuration
Ensure the game server configuration files are set correctly to support LiveAPI and RCON connections.

**LiveAPI Configuration**
File Path: `server_live_v2.6.21\platform\cfg\liveapi.cfg`

```cfg
liveapi_enabled           "1"                   // Enable LiveAPI functionality
liveapi_session_name      "cn-hangzhou-1"       // LiveAPI session name
liveapi_websocket_enabled "1"                   // Enable WebSocket transmission
liveapi_servers           "ws://127.0.0.1:7771" // WebSocket connection address (must match ws_host/ws_port in .env)
```

**RCON Configuration**
File Path: `server_live_v2.6.21\platform\cfg\tools\rcon_server.cfg`

```cfg
sv_rcon_password         "sv_rcon_password"                 // RCON password (must match r5_rcon_password in .env)
rcon_key                 "rcon_key"  // RCON key (note this is the server-side generated key, must match r5_rcon_key in .env)
```

## Deployment Steps

The following steps assume you have entered the project root directory in your terminal.

### 1. Install Dependencies
Install all packages in the workspace:
```shell
uv sync --all-packages
```

### 2. Start Services
Please start the following services in **three different terminal windows**.

#### Window 1: Start WebSocket Service
```shell
uv run python -m ws_service.main
```

#### Window 2: Start FastAPI Service
```shell
uv run python -m fastapi_service.main
```

#### Window 3: Start NoneBot Service
**Note**: The NoneBot service depends on the FastAPI service, please ensure the FastAPI service has started successfully.

Enter the NoneBot service directory, sync dependencies, and start:
```shell
cd services\nonebot_service
uv sync
nb run
```
