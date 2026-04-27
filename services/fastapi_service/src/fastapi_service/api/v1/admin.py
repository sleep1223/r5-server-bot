from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials
from shared_lib.config import settings
from shared_lib.models import BanRecord

from fastapi_service.core.auth import security_scheme, verify_token
from fastapi_service.core.cache import server_cache
from fastapi_service.core.constants import ALLOWED_REASONS, is_no_cover_allowed_server
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

    rcon_key, rcon_pwd = require_rcon_config()
    online_servers = server_cache.get_online_servers()
    target_loc, _ = player_service.get_online_location(player_obj)

    # NO_COVER: 缓存命中且目标服允许 NO_COVER 时跳过
    if reason == "NO_COVER" and target_loc and is_no_cover_allowed_server(target_loc.get("server_host"), target_loc.get("server_name")):
        return success(
            data={
                "player_online": True,
                "skipped": True,
                "skip_reason": "no_cover_allowed",
                "server": {"name": target_loc["server_name"], "host": target_loc["server_host"], "port": target_loc["server_port"]},
            },
            msg=f"Server {target_loc['server_name']} allows NO_COVER; kick skipped",
        )

    # NO_COVER: 过滤掉允许 NO_COVER 的服务器(如北京服)
    if reason == "NO_COVER":
        online_servers = [s for s in online_servers if not is_no_cover_allowed_server(s.get("server_host"), s.get("server_name"))]

    # 无论后续 RCON 是否成功,先统一记录一次踢出次数
    await admin_service.record_kick_offline(player_obj)

    if not online_servers:
        return success(
            data={
                "player_online": False,
                "broadcast_total": 0,
                "rcon_failed": True,
                "fail_reason": "no_online_servers",
            },
            msg=f"当前无可用在线服务器,RCON 踢出未执行;踢出次数仍已记录(原因 {reason})",
        )

    # 不依赖可能过期的位置缓存:并行尝试所有在线服务器,谁返回 kick 成功就是玩家真实所在服
    _, hit_server = await admin_service.broadcast_kick_player(player_obj.nucleus_id, reason, online_servers, rcon_key, rcon_pwd)

    if hit_server:
        # 命中后再把状态置为 kicked (record_kick_offline 已经把 kick_count + 1)
        await admin_service.mark_status_kicked(player_obj)
        return success(
            data={
                "player_online": True,
                "server": {"name": hit_server["server_name"], "host": hit_server["server_host"], "port": hit_server["server_port"]},
                "broadcast_total": len(online_servers),
            },
            msg=f"Player {player_obj.nucleus_id} kicked from {hit_server['server_name']} for {reason}",
        )

    # 没有任何服务器命中该玩家 → 视作离线 / 玩家列表尚未刷新到该玩家
    return success(
        data={
            "player_online": False,
            "broadcast_total": len(online_servers),
            "rcon_failed": True,
            "fail_reason": "no_server_hit",
        },
        msg=(
            f"已广播 {len(online_servers)} 台在线服务器,但均未命中玩家 {player_obj.nucleus_id}"
            f"(可能已离线或玩家列表尚未刷新);踢出次数已记录(原因 {reason})"
        ),
    )


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
    target_loc, _ = player_service.get_online_location(player_obj)

    # NO_COVER: 缓存命中且目标服允许 NO_COVER 时跳过
    if reason == "NO_COVER" and target_loc and is_no_cover_allowed_server(target_loc.get("server_host"), target_loc.get("server_name")):
        return success(
            data={
                "player_online": True,
                "skipped": True,
                "skip_reason": "no_cover_allowed",
                "primary_server": {"name": target_loc["server_name"], "host": target_loc["server_host"], "port": target_loc["server_port"]},
                "async_server_count": 0,
            },
            msg=f"Server {target_loc['server_name']} allows NO_COVER; ban skipped",
        )

    # NO_COVER: 过滤掉允许 NO_COVER 的服务器(如北京服)
    if reason == "NO_COVER":
        online_servers = [s for s in online_servers if not is_no_cover_allowed_server(s.get("server_host"), s.get("server_name"))]

    # 没有可用在线服务器: 仅记录封禁
    if not online_servers:
        await admin_service.record_ban(player_obj, reason, operator_name)
        return success(
            data={"player_online": False, "async_server_count": 0, "broadcast_total": 0},
            msg=f"No online servers; ban recorded for {player_obj.nucleus_id}",
        )

    # 不依赖可能过期的位置缓存:并行 bann (= kickid + banid) 所有在线服务器,
    # 这样玩家在哪台服都会被踢出,封禁列表也同步落地。
    online_server_key = f"{target_loc['server_host']}:{target_loc['server_port']}" if target_loc else None
    success_count, hits = await admin_service.broadcast_bann_player(
        player_obj.nucleus_id, reason, online_servers, rcon_key, rcon_pwd,
        online_server_key=online_server_key,
    )

    if success_count == 0:
        return error(
            ErrorCode.RCON_OPERATION_FAILED,
            msg=f"Failed to ban player {player_obj.nucleus_id} on any online server",
            data={"broadcast_total": len(online_servers)},
        )

    await admin_service.record_ban(player_obj, reason, operator_name)

    # primary_server: 优先用缓存定位,否则取任一命中
    primary: dict | None = None
    if target_loc:
        primary = {
            "server_name": target_loc["server_name"],
            "server_host": target_loc["server_host"],
            "server_port": target_loc["server_port"],
        }
    elif hits:
        primary = {
            "server_name": hits[0]["server_name"],
            "server_host": hits[0]["server_host"],
            "server_port": hits[0]["server_port"],
        }

    if primary:
        server_cache.cache_ban_location(
            player_obj.nucleus_id,
            server_name=primary["server_name"],
            server_host=primary["server_host"],
            server_port=primary["server_port"],
        )

    return success(
        data={
            "player_online": bool(target_loc),
            "primary_server": ({"name": primary["server_name"], "host": primary["server_host"], "port": primary["server_port"]} if primary else None),
            "async_server_count": max(0, success_count - 1),
            "broadcast_total": len(online_servers),
            "broadcast_success_count": success_count,
        },
        msg=f"Player {player_obj.nucleus_id} banned on {success_count}/{len(online_servers)} server(s) for {reason}",
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
