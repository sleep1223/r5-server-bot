# Copilot 指令

## 语言要求

- **所有对话、注释和文档使用中文**。代码中的变量名、函数名、类名保持英文。

## 项目概述

本项目是 R5 Reloaded（Apex Legends）游戏服务器管理平台，Python monorepo，使用 uv workspace 管理。

### 服务架构

- **ws_service** — WebSocket 服务，接收 LiveAPI protobuf 事件并持久化到数据库
- **fastapi_service** — HTTP API 服务，提供服务器/玩家数据查询，含后台任务（服务器列表轮询、IP 解析、玩家同步）
- **nonebot_service** — 聊天机器人（QQ/开黑啦），通过 HTTP 调用 fastapi_service（独立于主 workspace）
- **shared_lib** — 共享库（配置、数据库模型、迁移、工具函数），位于 `packages/shared_lib`

### 数据流

LiveAPI 事件 → ws_service → 数据库 ← fastapi_service（API + 后台任务） ← nonebot_service（聊天命令）

## 编码规范

### Python 版本与风格

- 使用 Python 3.12+ 特性
- 所有新增/修改的代码**必须有类型注解**
- 使用 `async/await` 编写异步代码
- ORM：Tortoise ORM（异步），开发环境 SQLite，生产环境 PostgreSQL

### 命名规范

- `snake_case` — 模块、函数、变量
- `PascalCase` — 类名
- `UPPER_SNAKE_CASE` — 常量

### 代码格式（ruff）

- 根目录行长限制：**200**
- `nonebot_service` 行长限制：**88**
- 导入排序遵循 ruff `I` 规则

### 禁止事项

- **不要**生成或修改 protobuf 文件（`*_pb2.py`、`*_pb2.pyi`）
- **不要**在代码中硬编码密钥或敏感信息
- 配置通过 `pydantic-settings` 从 `env/.env` 加载

## Commit 规范

格式：`type(scope): 中文摘要`

- **type**：`feat`、`fix`、`refactor`、`docs`
- **scope**：模块名，如 `fastapi`、`ws`、`nonebot`、`shared_lib`
- 摘要简洁描述改动原因，不超过 50 字

## 代码审查重点

- 类型注解是否完整
- 异步代码是否正确（避免阻塞调用）
- 安全性（SQL 注入、硬编码密钥、命令注入等）
- 是否符合命名规范和行长限制
- FastAPI 接口是否正确使用 Bearer Token 认证

## 测试规范

- 框架：`pytest` + `pytest-asyncio`
- 测试描述和注释使用中文，函数名使用英文 `snake_case`
- Mock 外部依赖（HTTP 请求等），集成测试使用真实数据库连接
