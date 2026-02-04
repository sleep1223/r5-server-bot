# R5-bot 部署指南

[English Version](README_en.md)

本指南介绍如何配置环境并启动 R5-bot 的各个服务组件。

## 环境准备

### 1. 配置 pip 镜像源（可选）
为了加速依赖包的下载，建议配置国内镜像源：
```shell
pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
```

### 2. 安装 uv
本项目使用 `uv` 进行依赖管理和运行：
```shell
pip install uv
```
*注意：安装完成后，请重启终端以确保 `uv` 命令生效。*

### 3. 安装 nb-cli
为了方便管理 NoneBot，建议安装 `nb-cli` 工具：
```shell
uv tool install nb-cli
```

## 配置

### 1. 环境变量配置
复制 `env/.env.example` 文件为 `env/.env`，并根据实际情况修改配置。

**`env/.env` 关键配置项说明：**
```properties
# 控制台输出的 netkey (对应 liveapi.cfg 中的配置)
r5_target_keys='["server_key"]'

# RCON 配置 (对应 rcon_server.cfg 中的配置)
r5_rcon_key="rcon_key"
r5_rcon_password="sv_rcon_password"

# WebSocket 服务设置
ws_host="127.0.0.1"
ws_port=7771

# FastAPI 服务设置
fastapi_host="0.0.0.0"
fastapi_port=8000
fastapi_access_tokens='["your_api_token"]'
```

### 2. 游戏服务器配置
请确保游戏服务器配置文件正确设置，以支持 LiveAPI 和 RCON 连接。

**LiveAPI 配置**
文件路径: `server_live_v2.6.21\platform\cfg\liveapi.cfg`

```cfg
liveapi_enabled           "1"                   // 启用 LiveAPI 功能
liveapi_session_name      "cn-hangzhou-1"       // LiveAPI 会话名称
liveapi_websocket_enabled "1"                   // 启用 WebSocket 传输
liveapi_servers           "ws://127.0.0.1:7771" // WebSocket 连接地址 (需与 .env 中的 ws_host/ws_port 对应)
```

**RCON 配置**
文件路径: `server_live_v2.6.21\platform\cfg\tools\rcon_server.cfg`

```cfg
sv_rcon_password         "sv_rcon_password"                 // RCON 密码 (需与 .env 中的 r5_rcon_password 对应)
rcon_key                 "rcon_key"  // RCON 密钥 (注意这是服务端生成的, 需与 .env 中的 r5_rcon_key 对应)
```

## 部署步骤

以下步骤假设您已在终端中进入项目根目录。

### 1. 安装依赖
安装工作区内的所有包：
```shell
uv sync --all-packages
```

### 2. 启动服务
请在 **三个不同的终端窗口** 中分别启动以下服务。

#### 窗口 1: 启动 WebSocket 服务
```shell
uv run python -m ws_service.main
```

#### 窗口 2: 启动 FastAPI 服务
```shell
uv run python -m fastapi_service.main
```

#### 窗口 3: 启动 NoneBot 服务
**注意**：NoneBot 服务依赖于 FastAPI 服务，请确保 FastAPI 服务已成功启动。

进入 NoneBot 服务目录，同步依赖并启动：
```shell
cd services\nonebot_service
uv sync
nb run
```
