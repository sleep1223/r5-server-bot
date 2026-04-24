import asyncio
import re
from datetime import datetime
from typing import Any

from loguru import logger
from shared_lib.config import settings
from shared_lib.models import IpInfo, Player
from shared_lib.utils.ip import resolve_ip
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q
from utils.netcon_client import R5NetConsole

from fastapi_service.core.cache import server_cache
from fastapi_service.core.utils import CN_TZ, generate_hash


async def _sync_one_server(
    s: dict,
    s_ip: str,
    s_port: int,
    rcon_key: str,
    rcon_pwd: str,
    ip_info_map: dict[str, IpInfo],
    all_players: list[Player],
    online_nucleus_ids: set[str],
) -> dict[str, Any]:
    client = R5NetConsole(s_ip, s_port, rcon_key)
    status_data: dict[str, Any] = {}
    try:
        proc_start = datetime.now()
        await client.connect()
        proc_duration = (datetime.now() - proc_start).total_seconds() * 1000
        status_data["server_ping"] = int(proc_duration)

        await client.authenticate_and_start(rcon_pwd)
        status_data.update(await client.get_status())

        players_data = status_data.get("players", [])
        status_data["_server"] = f"{s_ip}:{s_port}"
        status_data["_api_name"] = s.get("name", "Unknown Server")

        if not status_data.get("max_players"):
            try:
                status_data["max_players"] = int(s.get("maxPlayers", 0))
            except (ValueError, TypeError):
                status_data["max_players"] = 0

        full_name = status_data["_api_name"]
        match_complex = re.match(r"^\[(.*?)]\s*(.*?)\s*server QQ group\(\d+\)\s*(.*)$", full_name, re.IGNORECASE)
        if match_complex:
            status_data["short_name"] = f"[{match_complex.group(1)}] {match_complex.group(2)} {match_complex.group(3)}".strip()
        else:
            status_data["short_name"] = full_name
        ip_info = ip_info_map.get(s_ip)
        status_data["country"] = ip_info.country if ip_info else None
        status_data["region"] = ip_info.region if ip_info else None
        status_data["server_ping"] = status_data["server_ping"] or (ip_info.ping if ip_info else 0)
        status_data["ip"] = s_ip
        status_data["port"] = s_port
        for p_data in players_data:
            r_nucleus_id = p_data.get("uniqueid")
            if not r_nucleus_id:
                continue
            r_nucleus_hash = generate_hash(r_nucleus_id)
            online_nucleus_ids.add(r_nucleus_id)
            raw_ping = p_data.get("ping", 0)
            real_ping = raw_ping // 2
            p_data["ping"] = real_ping
            update_dict: dict[str, Any] = dict(
                nucleus_id=r_nucleus_id, nucleus_hash=r_nucleus_hash, ip=p_data.get("ip"), ping=real_ping, loss=p_data.get("loss", 0), status="online"
            )

            if p_data.get("ip"):
                ip_info_res = await asyncio.to_thread(resolve_ip, p_data.get("ip"))
                if ip_info_res:
                    if ip_info_res.get("country"):
                        update_dict["country"] = ip_info_res["country"]
                        p_data["country"] = ip_info_res["country"]
                    if ip_info_res.get("region"):
                        update_dict["region"] = ip_info_res["region"]
                        p_data["region"] = ip_info_res["region"]

            matched_player = None
            for p in all_players:
                if str(p.nucleus_id) == str(r_nucleus_id) or p.nucleus_hash == r_nucleus_hash:
                    matched_player = p
                    break
            if p_data.get("name"):
                update_dict["name"] = p_data.get("name")
            if matched_player and matched_player.online_at:
                p_data["online_at"] = matched_player.online_at
            else:
                p_data["online_at"] = datetime.now(CN_TZ)
            if matched_player:
                if not p_data.get("country") and matched_player.country:
                    p_data["country"] = matched_player.country
                if not p_data.get("region") and matched_player.region:
                    p_data["region"] = matched_player.region

                if matched_player.status != "online" or not matched_player.online_at:
                    update_dict["online_at"] = datetime.now(CN_TZ)
                    p_data["online_at"] = update_dict["online_at"]
                await matched_player.update_from_dict(update_dict).save()
            else:
                update_dict["online_at"] = datetime.now(CN_TZ)
                p_data["online_at"] = update_dict["online_at"]
                try:
                    new_player = await Player.create(**update_dict)
                    all_players.append(new_player)
                except IntegrityError:
                    existing_p = await Player.get_or_none(Q(nucleus_id=r_nucleus_id) | Q(nucleus_hash=r_nucleus_hash))
                    if existing_p:
                        await existing_p.update_from_dict(update_dict).save()
                        p_data["online_at"] = update_dict["online_at"]
                        all_players.append(existing_p)
    finally:
        await client.close()
    return status_data


async def sync_players_task() -> None:
    logger.info("Starting player sync background task...")
    target_keys = set(settings.r5_target_keys or [])
    rcon_key = settings.r5_rcon_key
    rcon_pwd = settings.r5_rcon_password
    if not rcon_key or not rcon_pwd:
        logger.warning("RCON sync disabled because r5_rcon_key or r5_rcon_password is empty")
        return
    while True:
        try:
            # 从 fetch_server_list_raw_task 填充的缓存读取，避免重复拉取远程接口
            raw = server_cache.raw_response
            raw_servers = raw.get("servers") if isinstance(raw, dict) else None
            server_list = list(raw_servers) if isinstance(raw_servers, list) else None
            if not server_list:
                logger.debug("Raw server list cache is empty, waiting for fetch_server_list_raw_task")
            if server_list is not None:
                try:
                    filtered_servers = []
                    for s in server_list:
                        name = s.get("name", "")
                        key = s.get("key", "")
                        if "CN" in name and (not target_keys or key in target_keys):
                            filtered_servers.append(s)
                    all_players = await Player.all()
                    server_ips = [s.get("ip") for s in filtered_servers if s.get("ip")]
                    ip_info_map = {}
                    if server_ips:
                        infos = await IpInfo.filter(ip__in=server_ips).all()
                        for i in infos:
                            ip_info_map[i.ip] = i
                    active_keys: set[str] = set()
                    online_nucleus_ids: set[str] = set()
                    any_server_failed = False
                    per_server_timeout = float(getattr(settings, "r5_rcon_per_server_timeout", 15))
                    for s in filtered_servers:
                        s_ip = s.get("ip")
                        try:
                            s_port = int(s.get("port"))
                        except (ValueError, TypeError):
                            continue
                        if not s_ip or not s_port:
                            continue
                        server_key = f"{s_ip}:{s_port}"
                        try:
                            status_data = await asyncio.wait_for(
                                _sync_one_server(s, s_ip, s_port, rcon_key, rcon_pwd, ip_info_map, all_players, online_nucleus_ids),
                                timeout=per_server_timeout,
                            )
                        except TimeoutError:
                            logger.error(f"Syncing {server_key} exceeded {per_server_timeout}s, skipping")
                            any_server_failed = True
                            continue
                        except Exception as e:
                            logger.error(f"Error syncing players from {server_key}: {e}")
                            any_server_failed = True
                            continue
                        active_keys.add(server_key)
                        # 单服成功后立刻写入缓存，避免被后续慢服务器拖住
                        server_cache.set_server(server_key, status_data)
                    server_cache.retain_servers(active_keys)
                    if not any_server_failed:
                        for p in all_players:
                            if p.nucleus_id and str(p.nucleus_id) not in online_nucleus_ids:
                                if p.status == "online":
                                    if p.online_at:
                                        session_seconds = int((datetime.now(CN_TZ) - p.online_at).total_seconds())
                                        if session_seconds > 0:
                                            p.total_playtime_seconds += session_seconds
                                    p.status = "offline"
                                    p.online_at = None  # type: ignore[reportAttributeAccessIssue]
                                    await p.save()
                                elif p.status == "banned":
                                    pass
                                if str(p.nucleus_id) not in online_nucleus_ids:
                                    if p.status not in ("offline", "banned"):
                                        p.status = "offline"
                                        p.online_at = None  # type: ignore[reportAttributeAccessIssue]
                                        await p.save()
                    else:
                        logger.warning("Skipping offline detection due to server sync failures")
                except Exception as e:
                    logger.error(f"Error updating rcon clients: {e}")
        except asyncio.CancelledError:
            logger.info("Player sync task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in sync_players_task: {e}")
        await asyncio.sleep(settings.r5_rcon_sync_interval)
