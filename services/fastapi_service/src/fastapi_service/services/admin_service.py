import asyncio

from loguru import logger
from shared_lib.config import settings
from shared_lib.models import BanRecord, Player
from tortoise.expressions import F

from fastapi_service.core.cache import server_cache

from .rcon import rcon_session


async def kick_player_on_server(nucleus_id: int, reason: str, host: str, port: int, rcon_key: str, rcon_pwd: str) -> bool:
    try:
        async with rcon_session(host, port, rcon_key, rcon_pwd) as client:
            return await client.kick(nucleus_id, f"#KICK_REASON_{reason}")
    except Exception as e:
        logger.error(f"Failed to kick player on {host}:{port}: {e}")
        return False


async def ban_player_on_server(nucleus_id: int, reason: str, host: str, port: int, rcon_key: str, rcon_pwd: str) -> bool:
    try:
        async with rcon_session(host, port, rcon_key, rcon_pwd) as client:
            return await client.ban(nucleus_id, f"BAN_REASON_{reason}")
    except Exception as e:
        logger.error(f"Failed to ban player on {host}:{port}: {e}")
        return False


async def unban_player_on_server(nucleus_id: int, host: str, port: int, rcon_key: str, rcon_pwd: str, *, timeout: float = 10.0) -> bool:
    try:
        async with rcon_session(host, port, rcon_key, rcon_pwd, timeout=timeout) as client:
            return await client.unban(nucleus_id)
    except Exception as e:
        logger.error(f"Failed to unban player on {host}:{port}: {e}")
        return False


async def record_ban(player: Player, reason: str, operator_name: str) -> None:
    await BanRecord.create(player=player, reason=reason, operator=operator_name)
    await Player.filter(id=player.id).update(ban_count=F("ban_count") + 1, status="banned")


async def record_kick(player: Player) -> None:
    await Player.filter(id=player.id).update(kick_count=F("kick_count") + 1, status="kicked")


async def record_kick_offline(player: Player) -> None:
    await Player.filter(id=player.id).update(kick_count=F("kick_count") + 1)


async def record_unban(nucleus_id: int) -> None:
    await Player.filter(nucleus_id=nucleus_id).update(status="offline")
    server_cache.clear_ban_location(nucleus_id)


# ── Background tasks ──


async def run_ban_on_servers_background(
    *,
    player_id: int,
    nucleus_id: int,
    reason: str,
    operator_name: str,
    servers: list[dict],
    update_record_on_success: bool,
    cache_server_on_first_success: bool,
) -> None:
    rcon_key = settings.r5_rcon_key
    rcon_pwd = settings.r5_rcon_password
    if not rcon_key or not rcon_pwd:
        logger.error("Background ban aborted: RCON configuration missing")
        return

    success_count = 0
    cached_server = False

    for server in servers:
        host = server["server_host"]
        port = server["server_port"]
        server_key = server["server_key"]
        server_name = server.get("server_name") or server_key
        try:
            async with rcon_session(host, port, rcon_key, rcon_pwd) as client:
                if await client.bann(nucleus_id, f"#BAN_REASON_{reason}"):
                    success_count += 1
                    if cache_server_on_first_success and not cached_server:
                        server_cache.cache_ban_location(nucleus_id, server_name=server_name, server_host=host, server_port=port)
                        cached_server = True
        except Exception as e:
            logger.error(f"Failed to ban player on {server_key}: {e}")

    if update_record_on_success and success_count > 0:
        player_obj = await Player.filter(id=player_id).first()
        if player_obj:
            await BanRecord.create(player=player_obj, reason=reason, operator=operator_name)
            await Player.filter(id=player_id).update(ban_count=F("ban_count") + 1, status="banned")

    logger.info(f"Background ban task finished for {nucleus_id}. success={success_count}/{len(servers)}")


async def run_unban_on_servers_background(
    *,
    player_id: int,
    nucleus_id: int,
    servers: list[dict],
) -> None:
    rcon_key = settings.r5_rcon_key
    rcon_pwd = settings.r5_rcon_password
    if not rcon_key or not rcon_pwd:
        logger.error("Background unban aborted: RCON configuration missing")
        return

    success_count = 0

    for server in servers:
        host = server["server_host"]
        port = server["server_port"]
        server_key = server["server_key"]
        try:
            async with rcon_session(host, port, rcon_key, rcon_pwd) as client:
                if await client.unban(nucleus_id):
                    success_count += 1
        except Exception as e:
            logger.error(f"Failed to unban player on {server_key}: {e}")

    if success_count > 0:
        await Player.filter(id=player_id).update(status="offline")
        server_cache.clear_ban_location(nucleus_id)

    logger.info(f"Background unban task finished for {nucleus_id}. success={success_count}/{len(servers)}")


def schedule_ban_background(
    *,
    player_id: int,
    nucleus_id: int,
    reason: str,
    operator_name: str,
    servers: list[dict],
    update_record_on_success: bool,
    cache_server_on_first_success: bool,
) -> None:
    asyncio.create_task(
        run_ban_on_servers_background(
            player_id=player_id,
            nucleus_id=nucleus_id,
            reason=reason,
            operator_name=operator_name,
            servers=servers,
            update_record_on_success=update_record_on_success,
            cache_server_on_first_success=cache_server_on_first_success,
        )
    )


def schedule_unban_background(*, player_id: int, nucleus_id: int, servers: list[dict]) -> None:
    asyncio.create_task(
        run_unban_on_servers_background(
            player_id=player_id,
            nucleus_id=nucleus_id,
            servers=servers,
        )
    )
