import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials
from loguru import logger
from shared_lib.config import settings
from shared_lib.models import BanRecord, Player

from ..auth import security_scheme, verify_token
from ..cache import banned_player_server_cache, global_server_cache
from ..constants import ALLOWED_REASONS
from ..errors import ErrorCode
from ..rcon import rcon_session, require_rcon_config
from ..response import error, paginated, success
from ..utils import CN_TZ, check_is_admin, parse_short_name
from .players import get_cached_ban_location, get_online_location, get_player_by_identifier

router = APIRouter()


def get_online_servers() -> list[dict]:
    servers = []
    for server_key, s_status in global_server_cache.items():
        try:
            host, port_str = str(server_key).rsplit(":", 1)
            port = int(port_str)
        except Exception:
            logger.error(f"Invalid server key in cache: {server_key}")
            continue

        servers.append({
            "server_key": str(server_key),
            "server_name": s_status.get("_api_name") or s_status.get("hostname") or str(server_key),
            "server_host": host,
            "server_port": port,
        })
    return servers


def cache_banned_server(nucleus_id: int, *, server_name: str, server_host: str, server_port: int) -> None:
    short_name = parse_short_name(server_name)
    banned_player_server_cache[nucleus_id] = {
        "server_name": server_name,
        "short_name": short_name,
        "server_host": server_host,
        "server_port": server_port,
        "cached_at": datetime.now(CN_TZ).isoformat(),
    }


def clear_cached_ban_location(nucleus_id: int) -> None:
    banned_player_server_cache.pop(nucleus_id, None)


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
                        cache_banned_server(
                            nucleus_id,
                            server_name=server_name,
                            server_host=host,
                            server_port=port,
                        )
                        cached_server = True
        except Exception as e:
            logger.error(f"Failed to ban player on {server_key}: {e}")

    if update_record_on_success and success_count > 0:
        player_obj = await Player.filter(id=player_id).first()
        if player_obj:
            await BanRecord.create(player=player_obj, reason=reason, operator=operator_name)
            await Player.filter(id=player_id).update(ban_count=player_obj.ban_count + 1, status="banned")

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
        clear_cached_ban_location(nucleus_id)

    logger.info(f"Background unban task finished for {nucleus_id}. success={success_count}/{len(servers)}")


@router.post("/players/{nucleus_id_or_player_name}/kick", dependencies=[Depends(verify_token)])
async def kick_player(
    nucleus_id_or_player_name: int | str,
    reason: str = Query(..., description=f"Reason for kick. Allowed: {ALLOWED_REASONS}"),
):
    if reason not in ALLOWED_REASONS:
        raise HTTPException(status_code=400, detail=f"Invalid reason. Allowed: {ALLOWED_REASONS}")

    player_obj, err = await get_player_by_identifier(nucleus_id_or_player_name)
    if err:
        return err
    assert player_obj is not None

    target_loc, err = get_online_location(player_obj)
    if err:
        await Player.filter(nucleus_id=player_obj.nucleus_id).update(kick_count=player_obj.kick_count + 1)
        return success(
            data={"player_online": False},
            msg=f"Player {player_obj.nucleus_id} is not online; kick count recorded for {reason}",
        )
    assert target_loc is not None

    target_host = target_loc["server_host"]
    target_port = target_loc["server_port"]

    rcon_key, rcon_pwd = require_rcon_config()

    kick_success = False
    try:
        async with rcon_session(target_host, target_port, rcon_key, rcon_pwd) as client:
            if await client.kick(player_obj.nucleus_id, f"#KICK_REASON_{reason}"):
                kick_success = True
    except Exception as e:
        logger.error(f"Failed to kick player on {target_host}:{target_port}: {e}")

    if kick_success:
        await Player.filter(nucleus_id=player_obj.nucleus_id).update(kick_count=player_obj.kick_count + 1, status="kicked")
        return success(
            data={
                "player_online": True,
                "server": {
                    "name": target_loc["server_name"],
                    "host": target_host,
                    "port": target_port,
                },
            },
            msg=f"Player {player_obj.nucleus_id} kicked from {target_loc['server_name']} for {reason}",
        )
    else:
        return error(ErrorCode.RCON_OPERATION_FAILED, msg=f"Failed to kick player {player_obj.nucleus_id}")


@router.post("/players/{nucleus_id_or_player_name}/ban", dependencies=[Depends(verify_token)])
async def ban_player(
    nucleus_id_or_player_name: int | str,
    reason: str = Query(..., description=f"Reason for ban. Allowed: {ALLOWED_REASONS}"),
    credentials: HTTPAuthorizationCredentials | None = Depends(verify_token),
):
    if reason not in ALLOWED_REASONS:
        raise HTTPException(status_code=400, detail=f"Invalid reason. Allowed: {ALLOWED_REASONS}")

    player_obj, err = await get_player_by_identifier(nucleus_id_or_player_name)
    if err:
        return err
    assert player_obj is not None

    rcon_key, rcon_pwd = require_rcon_config()

    online_servers = get_online_servers()

    operator_name = "admin" if credentials else "system"
    target_loc, online_error = get_online_location(player_obj)

    # 1) 玩家在线: 先同步封禁所在服务器；成功后异步在其余在线服务器补执行
    if not online_error and target_loc:
        target_host = target_loc["server_host"]
        target_port = target_loc["server_port"]
        target_server_name = target_loc["server_name"]

        ban_success = False
        try:
            async with rcon_session(target_host, target_port, rcon_key, rcon_pwd) as client:
                if await client.ban(player_obj.nucleus_id, f"BAN_REASON_{reason}"):
                    ban_success = True
        except Exception as e:
            logger.error(f"Failed to ban player on {target_host}:{target_port}: {e}")

        if not ban_success:
            return error(
                ErrorCode.RCON_OPERATION_FAILED,
                msg=f"Failed to ban player {player_obj.nucleus_id} on online server {target_server_name}",
                data={
                    "player_online": True,
                    "primary_server": {
                        "name": target_server_name,
                        "host": target_host,
                        "port": target_port,
                    },
                },
            )

        await BanRecord.create(player=player_obj, reason=reason, operator=operator_name)
        await Player.filter(nucleus_id=player_obj.nucleus_id).update(ban_count=player_obj.ban_count + 1, status="banned")
        cache_banned_server(
            player_obj.nucleus_id,
            server_name=target_server_name,
            server_host=target_host,
            server_port=target_port,
        )

        remain_servers = [s for s in online_servers if not (s["server_host"] == target_host and s["server_port"] == target_port)]
        if remain_servers:
            asyncio.create_task(
                run_ban_on_servers_background(
                    player_id=player_obj.id,
                    nucleus_id=player_obj.nucleus_id,
                    reason=reason,
                    operator_name=operator_name,
                    servers=remain_servers,
                    update_record_on_success=False,
                    cache_server_on_first_success=False,
                )
            )

        return success(
            data={
                "player_online": True,
                "primary_server": {
                    "name": target_server_name,
                    "host": target_host,
                    "port": target_port,
                },
                "async_server_count": len(remain_servers),
            },
            msg=f"Player {player_obj.nucleus_id} banned on online server {target_server_name}; background sync started",
        )

    # 2) 玩家不在线: 异步在所有在线服务器执行 bannid，任务启动即返回成功
    if online_servers:
        asyncio.create_task(
            run_ban_on_servers_background(
                player_id=player_obj.id,
                nucleus_id=player_obj.nucleus_id,
                reason=reason,
                operator_name=operator_name,
                servers=online_servers,
                update_record_on_success=True,
                cache_server_on_first_success=True,
            )
        )

    return success(
        data={
            "player_online": False,
            "async_server_count": len(online_servers),
        },
        msg=f"Player {player_obj.nucleus_id} is not online; background ban task started",
    )


@router.post("/players/{nucleus_id_or_player_name}/unban", dependencies=[Depends(verify_token)])
async def unban_player(nucleus_id_or_player_name: int | str):
    player_obj, err = await get_player_by_identifier(nucleus_id_or_player_name)
    if err:
        return err
    assert player_obj is not None

    rcon_key, rcon_pwd = require_rcon_config()

    online_servers = get_online_servers()
    target_loc, online_error = get_online_location(player_obj)
    if online_error and player_obj.nucleus_id:
        cached_target_loc = get_cached_ban_location(player_obj.nucleus_id)
        if cached_target_loc:
            target_loc = cached_target_loc
            online_error = None

    # 1) 玩家在线或命中封禁缓存服务器: 先同步解封目标服务器；成功后异步在其余在线服务器补执行
    if not online_error and target_loc:
        target_host = target_loc["server_host"]
        target_port = target_loc["server_port"]
        target_server_name = target_loc["server_name"]
        target_source = "ban_cache" if target_loc.get("_from_ban_cache") else "online"
        target_source_desc = "cached ban server" if target_source == "ban_cache" else "online server"

        unban_success = False
        try:
            async with rcon_session(target_host, target_port, rcon_key, rcon_pwd, timeout=1.0) as client:
                if await client.unban(player_obj.nucleus_id):
                    unban_success = True
        except Exception as e:
            logger.error(f"Failed to unban player on {target_host}:{target_port}: {e}")

        if unban_success:
            await Player.filter(nucleus_id=player_obj.nucleus_id).update(status="offline")
            clear_cached_ban_location(player_obj.nucleus_id)

            remain_servers = [s for s in online_servers if not (s["server_host"] == target_host and s["server_port"] == target_port)]
            if remain_servers:
                asyncio.create_task(
                    run_unban_on_servers_background(
                        player_id=player_obj.id,
                        nucleus_id=player_obj.nucleus_id,
                        servers=remain_servers,
                    )
                )

            return success(
                data={
                    "player_online": target_source == "online",
                    "target_source": target_source,
                    "target_server": {
                        "name": target_server_name,
                        "host": target_host,
                        "port": target_port,
                    },
                    "async_server_count": len(remain_servers),
                },
                msg=f"Player {player_obj.nucleus_id} unbanned on {target_source_desc} {target_server_name}; background sync started",
            )

        if target_source == "online":
            return error(
                ErrorCode.RCON_OPERATION_FAILED,
                msg=f"Failed to unban player {player_obj.nucleus_id} on {target_source_desc} {target_server_name}",
                data={
                    "player_online": True,
                    "target_source": target_source,
                    "target_server": {
                        "name": target_server_name,
                        "host": target_host,
                        "port": target_port,
                    },
                },
            )

        logger.warning(f"Failed to unban player {player_obj.nucleus_id} on cached ban server {target_server_name}, fallback to background unban on online servers")

    # 2) 玩家不在线: 异步在所有在线服务器执行 unban，任务启动即返回成功
    if online_servers:
        asyncio.create_task(
            run_unban_on_servers_background(
                player_id=player_obj.id,
                nucleus_id=player_obj.nucleus_id,
                servers=online_servers,
            )
        )
        return success(
            data={
                "player_online": False,
                "async_server_count": len(online_servers),
            },
            msg=f"Player {player_obj.nucleus_id} is not online; background unban task started",
        )

    return error(ErrorCode.NO_ONLINE_SERVERS, msg="No online servers found to execute unban")


@router.get("/bans")
async def get_ban_list(
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
):
    """获取封禁记录列表。"""
    is_admin = check_is_admin(credentials, settings.fastapi_access_tokens)

    total = await BanRecord.all().count()
    offset = (page_no - 1) * page_size
    bans = await BanRecord.all().order_by("-created_at").offset(offset).limit(page_size).prefetch_related("player")

    results = []
    for ban in bans:
        player_info = {
            "id": ban.id,
            "player": {
                "name": ban.player.name,
                "nucleus_id": ban.player.nucleus_id,
                "kick_count": ban.player.kick_count,
                "ban_count": ban.player.ban_count,
                "status": ban.player.status,
                "country": ban.player.country,
            },
            "reason": ban.reason,
            "operator": ban.operator,
            "created_at": ban.created_at,
        }
        if is_admin:
            player_info["player"].update({
                "region": ban.player.region,
            })
        results.append(player_info)

    return paginated(data=results, total=total, msg="Ban list retrieved")
