from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials
from loguru import logger
from shared_lib.config import settings

from ..auth import security_scheme, verify_token
from ..cache import global_server_cache, raw_server_response_cache
from ..response import success
from ..utils import check_is_admin, parse_short_name

router = APIRouter()


@router.get("/server")
async def get_raw_server_list():
    """获取原始服务器列表缓存数据，无需鉴权"""
    return success(data=raw_server_response_cache, msg="Raw server list retrieved")


@router.get("/server/info", dependencies=[Depends(verify_token)])
async def get_server_info():
    if not global_server_cache:
        pass

    results = list(global_server_cache.values())
    return success(data=results, msg="Server info retrieved")


@router.get("/server/status")
async def get_server_status(
    server_name: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
):
    """
    获取所有已连接服务器或特定服务器的状态。
    返回服务器名称（缩写）、在线玩家数量和 Ping。
    """
    is_admin = check_is_admin(credentials, settings.fastapi_access_tokens)

    results = []

    for server_cache in global_server_cache.values():
        try:
            full_name = server_cache.get("_api_name") or server_cache.get("hostname") or server_cache.get("_server", "Unknown")

            if server_name and server_name.lower() not in full_name.lower():
                continue

            short_name = server_cache.get("short_name")
            if not short_name:
                short_name = parse_short_name(full_name)

            player_count = len(server_cache.get("players", []))

            host_str = server_cache.get("_server", "")
            host_ip = server_cache.get("ip")
            if not host_ip and host_str:
                host_ip = host_str.split(":")[0]

            host_port = server_cache.get("port")
            if not host_port and host_str and ":" in host_str:
                try:
                    host_port = int(host_str.split(":")[1])
                except Exception:
                    host_port = 0

            country = server_cache.get("country")
            region = server_cache.get("region")
            server_ping = server_cache.get("server_ping", 0)
            max_players = server_cache.get("max_players", 0)

            player_list = []
            for p in server_cache.get("players", []):
                p_info = {"name": p.get("name", "Unknown")}
                p_info["country"] = p.get("country")
                if is_admin:
                    p_info["region"] = p.get("region")
                player_list.append(p_info)

            results.append({
                "name": full_name,
                "short_name": short_name,
                "full_name": full_name,
                "player_count": player_count,
                "max_players": max_players,
                "ping": server_ping,
                "ip": host_ip,
                "port": host_port,
                "country": country,
                "region": region,
                "host": host_str,
                "players": player_list,
            })

        except Exception as e:
            logger.error(f"Error processing status for {server_cache.get('_server')}: {e}")
            continue

    return success(data=results, msg=f"Server status for {len(results)} servers")
