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
# 远程服务器列表
r5_servers_url="https://r5r-org.sleep0.de/servers"
r5_servers_fetch_interval=180

# FastAPI 服务设置
fastapi_host="0.0.0.0"
fastapi_port=8000
fastapi_access_tokens='["your_api_token"]'

# KD/武器等战绩统计排除的服务器 IP
kd_excluded_server_hosts='["47.116.182.240"]'
```

## 部署步骤

以下步骤假设您已在终端中进入项目根目录。

### 1. 安装依赖

安装工作区内的所有包：

```shell
uv sync --all-packages
```

### 2. 启动服务

请在不同的终端窗口中分别启动以下服务。

```shell
uv run python -m fastapi_service.server
```

#### 启动 NoneBot 服务

**注意**：NoneBot 服务依赖于 FastAPI 服务，请确保 FastAPI 服务已成功启动。

进入 NoneBot 服务目录，同步依赖并启动：

```shell
cd services\nonebot_service
uv sync
nb run
```
