from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="env/.env", env_ignore_empty=True, extra="ignore")

    # Database settings
    db_url: str = "sqlite://db.sqlite3"

    # WS Service settings
    ws_host: str = "0.0.0.0"
    ws_port: int = 8000

    # FastAPI Service settings
    fastapi_host: str = "0.0.0.0"
    fastapi_port: int = 8000

    # R5 Service settings
    r5_servers_url: str = "https://r5r-sl.ugniushosting.com/servers"
    r5_target_keys: list[str] = []
    r5_rcon_key: str = ""
    r5_rcon_password: str = ""

    api_tokens: list[str] = []

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
