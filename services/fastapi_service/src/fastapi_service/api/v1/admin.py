from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials
from shared_lib.config import settings
from shared_lib.models import BanRecord

from fastapi_service.core.auth import security_scheme, verify_token
from fastapi_service.core.cache import server_cache
from fastapi_service.core.constants import ALLOWED_REASONS
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, paginated, success
from fastapi_service.core.utils import check_is_admin
from fastapi_service.services import admin_service, player_service
from fastapi_service.services.rcon import require_rcon_config

from ..deps import Pagination, get_pagination

router = APIRouter()


@router.post("/players/{nucleus_id_or_player_name}/kick", dependencies=[Depends(verify_token)])
async def kick_player(
    nucleus_id_or_player_name: int | str,
    reason: str = Query(..., description=f"Reason for kick. Allowed: {ALLOWED_REASONS}"),
):
    if reason not in ALLOWED_REASONS:
        raise HTTPException(status_code=400, detail=f"Invalid reason. Allowed: {ALLOWED_REASONS}")

    player_obj, err = await player_service.get_player_by_identifier(nucleus_id_or_player_name)
    if err:
        return err
    assert player_obj is not None

    target_loc, err = player_service.get_online_location(player_obj)
    if err:
        await admin_service.record_kick_offline(player_obj)
        return success(
            data={"player_online": False},
            msg=f"Player {player_obj.nucleus_id} is not online; kick count recorded for {reason}",
        )
    assert target_loc is not None

    rcon_key, rcon_pwd = require_rcon_config()
    kick_success = await admin_service.kick_player_on_server(player_obj.nucleus_id, reason, target_loc["server_host"], target_loc["server_port"], rcon_key, rcon_pwd)

    if kick_success:
        await admin_service.record_kick(player_obj)
        return success(
            data={
                "player_online": True,
                "server": {"name": target_loc["server_name"], "host": target_loc["server_host"], "port": target_loc["server_port"]},
            },
            msg=f"Player {player_obj.nucleus_id} kicked from {target_loc['server_name']} for {reason}",
        )
    return error(ErrorCode.RCON_OPERATION_FAILED, msg=f"Failed to kick player {player_obj.nucleus_id}")


@router.post("/players/{nucleus_id_or_player_name}/ban", dependencies=[Depends(verify_token)])
async def ban_player(
    nucleus_id_or_player_name: int | str,
    reason: str = Query(..., description=f"Reason for ban. Allowed: {ALLOWED_REASONS}"),
    credentials: HTTPAuthorizationCredentials | None = Depends(verify_token),
):
    if reason not in ALLOWED_REASONS:
        raise HTTPException(status_code=400, detail=f"Invalid reason. Allowed: {ALLOWED_REASONS}")

    player_obj, err = await player_service.get_player_by_identifier(nucleus_id_or_player_name)
    if err:
        return err
    assert player_obj is not None

    rcon_key, rcon_pwd = require_rcon_config()
    online_servers = server_cache.get_online_servers()
    operator_name = "admin" if credentials else "system"
    target_loc, online_error = player_service.get_online_location(player_obj)

    # 1) 玩家在线: 先同步封禁所在服务器
    if not online_error and target_loc:
        target_host = target_loc["server_host"]
        target_port = target_loc["server_port"]
        target_server_name = target_loc["server_name"]

        ban_success = await admin_service.ban_player_on_server(player_obj.nucleus_id, reason, target_host, target_port, rcon_key, rcon_pwd)

        if not ban_success:
            return error(
                ErrorCode.RCON_OPERATION_FAILED,
                msg=f"Failed to ban player {player_obj.nucleus_id} on online server {target_server_name}",
                data={"player_online": True, "primary_server": {"name": target_server_name, "host": target_host, "port": target_port}},
            )

        await admin_service.record_ban(player_obj, reason, operator_name)
        server_cache.cache_ban_location(player_obj.nucleus_id, server_name=target_server_name, server_host=target_host, server_port=target_port)

        remain_servers = [s for s in online_servers if not (s["server_host"] == target_host and s["server_port"] == target_port)]
        if remain_servers:
            admin_service.schedule_ban_background(
                player_id=player_obj.id,
                nucleus_id=player_obj.nucleus_id,
                reason=reason,
                operator_name=operator_name,
                servers=remain_servers,
                update_record_on_success=False,
                cache_server_on_first_success=False,
            )

        return success(
            data={"player_online": True, "primary_server": {"name": target_server_name, "host": target_host, "port": target_port}, "async_server_count": len(remain_servers)},
            msg=f"Player {player_obj.nucleus_id} banned on online server {target_server_name}; background sync started",
        )

    # 2) 玩家不在线: 立即记录封禁，再异步在所有在线服务器执行 RCON
    await admin_service.record_ban(player_obj, reason, operator_name)

    if online_servers:
        admin_service.schedule_ban_background(
            player_id=player_obj.id,
            nucleus_id=player_obj.nucleus_id,
            reason=reason,
            operator_name=operator_name,
            servers=online_servers,
            update_record_on_success=False,
            cache_server_on_first_success=True,
        )

    return success(
        data={"player_online": False, "async_server_count": len(online_servers)},
        msg=f"Player {player_obj.nucleus_id} is not online; ban recorded, background RCON task started",
    )


@router.post("/players/{nucleus_id_or_player_name}/unban", dependencies=[Depends(verify_token)])
async def unban_player(nucleus_id_or_player_name: int | str):
    player_obj, err = await player_service.get_player_by_identifier(nucleus_id_or_player_name)
    if err:
        return err
    assert player_obj is not None

    rcon_key, rcon_pwd = require_rcon_config()
    online_servers = server_cache.get_online_servers()
    target_loc, online_error = player_service.get_online_location(player_obj)

    if online_error and player_obj.nucleus_id:
        cached_target_loc = player_service.get_cached_ban_location(player_obj.nucleus_id)
        if cached_target_loc:
            target_loc = cached_target_loc
            online_error = None

    # 1) 玩家在线或命中封禁缓存服务器
    if not online_error and target_loc:
        target_host = target_loc["server_host"]
        target_port = target_loc["server_port"]
        target_server_name = target_loc["server_name"]
        target_source = "ban_cache" if target_loc.get("_from_ban_cache") else "online"
        target_source_desc = "cached ban server" if target_source == "ban_cache" else "online server"

        unban_success = await admin_service.unban_player_on_server(player_obj.nucleus_id, target_host, target_port, rcon_key, rcon_pwd, timeout=1.0)

        if unban_success:
            await admin_service.record_unban(player_obj.nucleus_id)
            remain_servers = [s for s in online_servers if not (s["server_host"] == target_host and s["server_port"] == target_port)]
            if remain_servers:
                admin_service.schedule_unban_background(player_id=player_obj.id, nucleus_id=player_obj.nucleus_id, servers=remain_servers)

            return success(
                data={
                    "player_online": target_source == "online",
                    "target_source": target_source,
                    "target_server": {"name": target_server_name, "host": target_host, "port": target_port},
                    "async_server_count": len(remain_servers),
                },
                msg=f"Player {player_obj.nucleus_id} unbanned on {target_source_desc} {target_server_name}; background sync started",
            )

        if target_source == "online":
            return error(
                ErrorCode.RCON_OPERATION_FAILED,
                msg=f"Failed to unban player {player_obj.nucleus_id} on {target_source_desc} {target_server_name}",
                data={"player_online": True, "target_source": target_source, "target_server": {"name": target_server_name, "host": target_host, "port": target_port}},
            )

    # 2) 玩家不在线: 异步在所有在线服务器执行 unban
    if online_servers:
        admin_service.schedule_unban_background(player_id=player_obj.id, nucleus_id=player_obj.nucleus_id, servers=online_servers)
        return success(
            data={"player_online": False, "async_server_count": len(online_servers)},
            msg=f"Player {player_obj.nucleus_id} is not online; background unban task started",
        )

    return error(ErrorCode.NO_ONLINE_SERVERS, msg="No online servers found to execute unban")


@router.get("/bans")
async def get_ban_list(
    pg: Pagination = Depends(get_pagination),
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
):
    """获取封禁记录列表。"""
    is_admin = check_is_admin(credentials, settings.fastapi_access_tokens)

    total = await BanRecord.all().count()
    bans = await BanRecord.all().order_by("-created_at").offset(pg.offset).limit(pg.page_size).prefetch_related("player")

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
            player_info["player"].update({"region": ban.player.region})
        results.append(player_info)

    return paginated(data=results, total=total, msg="Ban list retrieved")
