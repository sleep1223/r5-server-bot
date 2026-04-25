from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="env/.env", env_ignore_empty=True, extra="ignore")

    # Database settings
    db_url: str = "sqlite://db.sqlite3"

    # WS Service settings
    ws_host: str = "0.0.0.0"
    ws_port: int = 8000
    ws_batch_interval: int = 30  # 批量上报间隔（秒）
    ws_batch_max_retries: int = 3  # 批量上报失败最大重试次数
    # 本机在外网的 IP；不设则启动时通过公共探测接口自动解析
    ws_public_ip: str = ""
    # 游戏服对外端口（填入 Server.port 初值）
    ws_public_port: int = 37015
    # WS 上报给 FastAPI 的 HTTP 接口前缀（带 /v1/r5/ingest）
    ws_ingest_base_url: str = "http://127.0.0.1:8000/v1/r5/ingest"
    # Bearer token，必须是 fastapi_access_tokens 中的一个
    ws_ingest_token: str = ""
    # 单次 POST 超时时间（秒）
    ws_ingest_timeout: float = 15.0
    # 内存缓冲的最大事件数量，超出后丢弃最旧事件（避免上报长时间失败导致 OOM）
    ws_ingest_buffer_max: int = 100000

    # FastAPI Service settings
    fastapi_host: str = "0.0.0.0"
    fastapi_port: int = 8000
    fastapi_cors_origins: list[str] = ["*"]
    fastapi_access_tokens: list[str] = []
    # Granian TCP backlog / backpressure。workers 固定 1（ASGI 单事件循环）。
    fastapi_backlog: int = 1024
    fastapi_backpressure: int = 128
    # Ingest 子服务：独立 Granian 进程 (workers=1) 避免进程内缓存/后台任务重复
    fastapi_ingest_host: str = "0.0.0.0"
    fastapi_ingest_port: int = 8010

    # R5 Service settings
    r5_servers_url: str = "https://r5r-sl.ugniushosting.com/servers"
    # 拉取 r5_servers_url 远程服务器列表的时间间隔（秒）
    r5_servers_fetch_interval: int = 180
    r5_target_keys: list[str] = []
    r5_rcon_key: str = ""
    r5_rcon_password: str = ""
    # RCON 同步玩家列表的时间间隔（秒）
    r5_rcon_sync_interval: int = 30
    # 单台服务器 RCON 同步的总超时（秒），超过则跳过该服务器，避免拖住整轮
    r5_rcon_per_server_timeout: float = 15.0

    # 无规则服务器(如北京服): 命中以下任一规则的服务器会跳过 NO_COVER(撤回掩体) 的 kick/ban 后台执行
    no_cover_allowed_server_hosts: list[str] = ["106.75.50.197"]
    no_cover_allowed_server_name_markers: list[str] = ["[CN(Beijing)]", "No Rules"]

    # KD/武器等战绩统计排除的服务器 IP（如无规则/纯娱乐服），命中则该服务器的击杀记录不计入统计
    kd_excluded_server_hosts: list[str] = []

    # Data settings
    qqwry_path: str = "services/fastapi_service/data/qqwry.dat"
    launcher_config_path: str = "services/fastapi_service/data/launcher_config.toml"
    launcher_update_path: str = "services/fastapi_service/data/launcher_update.toml"

    # Match tracking settings
    # 活跃 match 超过此秒数未关闭即标记为 abandoned（safety net，典型 BR 一场 ~25min）
    match_inactivity_timeout_seconds: int = 7200
    # 更积极的"无活动"关闭：无击杀超过此秒数（默认 30min）→ 标记 completed/no_activity
    # 覆盖"玩家全退 → 状态机 Prematch 信号不再到达"这类场景
    match_no_activity_timeout_seconds: int = 1800
    # close_stale_matches 后台任务扫描间隔（秒）
    match_closer_interval_seconds: int = 60
    # reconcile_matches 对账任务：周期间隔 + 宽限期（防止跟事件驱动路径抢）
    match_reconcile_interval_seconds: int = 180
    match_reconcile_grace_seconds: int = 120
    # /对局：每场 top1 击杀数低于此值则不显示
    recent_match_top_kills_threshold: int = 50
    # /竞技：每人每天按击杀取前 N 场计入周榜
    competitive_daily_match_limit: int = 3
    # /个人对局：默认返回最近 N 场
    personal_match_default_limit: int = 3

    # Steam authentication / pylon master-server settings
    steam_web_api_key: str = ""
    steam_app_id: str = "480"  # Spacewar fallback for testing
    steam_auth_timeout: float = 7.0
    steam_persona_lookup: bool = True
    jwt_private_key_path: str = "services/fastapi_service/data/jwt_private.pem"
    jwt_public_key_path: str = "services/fastapi_service/data/jwt_public.pem"
    jwt_private_key_passphrase: str = ""
    jwt_token_ttl_seconds: int = 30
    pylon_default_server_port: int = 37015

    @property
    def tortoise_orm(self) -> dict:
        return {
            "connections": {"default": self.db_url},
            "apps": {
                "models": {
                    "models": ["shared_lib.models", "aerich.models"],
                    "default_connection": "default",
                }
            },
            "use_tz": False,
            "timezone": "Asia/Shanghai",
        }


settings = Settings()
