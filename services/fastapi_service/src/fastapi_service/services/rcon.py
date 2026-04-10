from contextlib import asynccontextmanager

from fastapi import HTTPException
from shared_lib.config import settings
from utils.netcon_client import R5NetConsole


def require_rcon_config() -> tuple[str, str]:
    rcon_key = settings.r5_rcon_key
    rcon_pwd = settings.r5_rcon_password
    if not rcon_key or not rcon_pwd:
        raise HTTPException(status_code=503, detail="RCON configuration missing")
    return rcon_key, rcon_pwd


@asynccontextmanager
async def rcon_session(host: str, port: int, rcon_key: str, rcon_pwd: str, *, timeout: float = 10.0):
    client = R5NetConsole(host, port, rcon_key)
    try:
        await client.connect(timeout=timeout)
        await client.authenticate_and_start(rcon_pwd)
        yield client
    finally:
        await client.close()
