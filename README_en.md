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
# Remote server list
r5_servers_url="https://r5r-org.sleep0.de/servers"
r5_servers_fetch_interval=180

# FastAPI Service settings
fastapi_host="0.0.0.0"
fastapi_port=8000
fastapi_access_tokens='["your_api_token"]'

# Server IPs excluded from KD / weapon statistics
kd_excluded_server_hosts='["47.116.182.240"]'
```

## Deployment Steps

The following steps assume you have entered the project root directory in your terminal.

### 1. Install Dependencies

Install all packages in the workspace:

```shell
uv sync --all-packages
```

### 2. Start Services

Start the following services in separate terminal windows.

```shell
uv run python -m fastapi_service.server
```

#### Start NoneBot Service

**Note**: The NoneBot service depends on the FastAPI service, please ensure the FastAPI service has started successfully.

Enter the NoneBot service directory, sync dependencies, and start:

```shell
cd services\nonebot_service
uv sync
nb run
```
