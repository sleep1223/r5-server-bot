from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="env/.env", env_ignore_empty=True, extra="ignore")

    # Database settings
    db_url: str = "sqlite://db.sqlite3"

    # WS Service settings
    ws_host: str = "0.0.0.0"
    ws_port: int = 8000
    ws_batch_interval: int = 60  # 批量写入间隔（秒）
    ws_batch_max_retries: int = 3  # 批量写入失败最大重试次数

    # FastAPI Service settings
    fastapi_host: str = "0.0.0.0"
    fastapi_port: int = 8000
    fastapi_cors_origins: list[str] = ["*"]
    fastapi_access_tokens: list[str] = []

    # R5 Service settings
    r5_servers_url: str = "https://r5r-sl.ugniushosting.com/servers"
    # 拉取 r5_servers_url 远程服务器列表的时间间隔（秒）
    r5_servers_fetch_interval: int = 180
    r5_target_keys: list[str] = []
    r5_rcon_key: str = ""
    r5_rcon_password: str = ""

    # 无规则服务器(如北京服): 命中以下任一规则的服务器会跳过 NO_COVER(撤回掩体) 的 kick/ban 后台执行
    no_cover_allowed_server_hosts: list[str] = ["106.75.50.197"]
    no_cover_allowed_server_name_markers: list[str] = ["[CN(Beijing)]"]

    # Data settings
    qqwry_path: str = "services/fastapi_service/data/qqwry.dat"
    launcher_config_path: str = "services/fastapi_service/data/launcher_config.toml"
    launcher_update_path: str = "services/fastapi_service/data/launcher_update.toml"

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
