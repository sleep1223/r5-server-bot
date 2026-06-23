from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="env/.env", env_ignore_empty=True, extra="ignore")

    # 日志等级 (TRACE/DEBUG/INFO/SUCCESS/WARNING/ERROR/CRITICAL)
    log_level: str = "INFO"

    # Database settings
    db_url: str = "sqlite://db.sqlite3"

    # FastAPI Service settings
    fastapi_host: str = "0.0.0.0"
    fastapi_port: int = 8000
    fastapi_cors_origins: list[str] = ["*"]
    fastapi_access_tokens: list[str] = []
    # Granian TCP backlog / backpressure。workers 固定 1（ASGI 单事件循环）。
    fastapi_backlog: int = 1024
    fastapi_backpressure: int = 128

    # R5 Service settings
    r5_servers_url: str = "https://r5r-sl.ugniushosting.com/servers"
    # 拉取 r5_servers_url 远程服务器列表的时间间隔（秒）
    r5_servers_fetch_interval: int = 180

    # KD/武器等战绩统计排除的服务器 IP（如无规则/纯娱乐服），命中则该服务器的击杀记录不计入统计
    kd_excluded_server_hosts: list[str] = []
    # 玩家每日击杀统计缓存刷新间隔（秒）
    player_kill_daily_stats_refresh_interval_seconds: int = 300
    # 每轮刷新回看最近 N 天，覆盖迟到写入和短时间修正
    player_kill_daily_stats_refresh_lookback_days: int = 2
    # 单次历史回填的天数上限，避免一次任务扫太久
    player_kill_daily_stats_backfill_batch_days: int = 7

    # Data settings
    qqwry_path: str = "services/fastapi_service/data/qqwry.dat"
    launcher_config_path: str = "services/fastapi_service/data/launcher_config.toml"
    launcher_update_path: str = "services/fastapi_service/data/launcher_update.toml"
    # 启动器最新版本号优先来源：GitHub Releases。失败或未配置时回退到 launcher_update.toml 的 latest
    launcher_github_repo: str = "sleep1223/r5r-cn-launcher"
    launcher_github_fetch_interval: int = 600

    # Match tracking settings
    # 活跃 match 超过此秒数未关闭即标记为 abandoned（safety net，典型 BR 一场 ~25min）
    match_inactivity_timeout_seconds: int = 7200
    # 更积极的"无活动"关闭：无击杀超过此秒数（默认 30min）→ 标记 completed/no_activity
    # 覆盖"玩家全退 → 状态机 Prematch 信号不再到达"这类场景
    match_no_activity_timeout_seconds: int = 1800
    # close_stale_matches 后台任务扫描间隔（秒）
    match_closer_interval_seconds: int = 60
    # /对局：每场 top1 击杀数低于此值则不显示
    recent_match_top_kills_threshold: int = 50
    # /竞技：每人每天按击杀取前 N 场计入周榜
    competitive_daily_match_limit: int = 3
    # /个人对局：默认返回最近 N 场
    personal_match_default_limit: int = 3

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
