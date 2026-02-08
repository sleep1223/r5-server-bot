import asyncio
import re
from datetime import datetime
from typing import Any

import httpx
from loguru import logger
from shared_lib.config import settings
from shared_lib.models import IpInfo, Player
from shared_lib.utils.ip import resolve_ip
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from .cache import global_server_cache, raw_server_response_cache
from .netcon_client import R5NetConsole
from .utils import CN_TZ, generate_hash, get_local_ping, resolve_ips_batch


async def fetch_server_list_raw_task():
    url = "https://r5r-sl.ugniushosting.com/servers"
    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.post(url, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    raw_server_response_cache.clear()
                    if isinstance(data, dict):
                        raw_server_response_cache.update(data)
                    else:
                        raw_server_response_cache["data"] = data
                else:
                    logger.warning(f"Failed to fetch raw server list: {response.status_code}")
            except Exception as e:
                logger.error(f"Error fetching raw server list: {e}")
            await asyncio.sleep(5)


async def ip_resolution_task():
    while True:
        try:
            server_ips = set()
            ips_to_process = set()
            for server_cache in global_server_cache.values():
                host_str = server_cache.get("_server")
                if host_str:
                    ip = host_str.split(":")[0]
                    server_ips.add(ip)
                    ips_to_process.add(ip)
            players = await Player.filter(ip__isnull=False).all()
            player_ip_map = {}
            for p in players:
                if p.ip:
                    ips_to_process.add(p.ip)
                    player_ip_map[p.ip] = p
            existing_infos = await IpInfo.filter(ip__in=list(ips_to_process)).all()
            existing_ip_map = {info.ip: info for info in existing_infos}
            missing_in_db = [ip for ip in ips_to_process if ip not in existing_ip_map]
            if missing_in_db:
                await IpInfo.bulk_create([IpInfo(ip=ip, is_resolved=False) for ip in missing_in_db])
                existing_infos = await IpInfo.filter(ip__in=list(ips_to_process)).all()
                existing_ip_map = {info.ip: info for info in existing_infos}
            players_by_ip = {}
            for p in players:
                if p.ip:
                    players_by_ip.setdefault(p.ip, []).append(p)
            for ip, p_list in players_by_ip.items():
                if ip in existing_ip_map:
                    info = existing_ip_map[ip]
                    try:
                        await info.players.add(*p_list)
                    except Exception:
                        pass
            target_ips = set()
            for info in existing_infos:
                if not info.is_resolved or not info.country or not info.region:
                    target_ips.add(info.ip)
            if target_ips:
                resolved_data = await resolve_ips_batch(list(target_ips))
                for ip, data in resolved_data.items():
                    try:
                        if ip in existing_ip_map:
                            info = existing_ip_map[ip]
                            info.country = data.get("country")
                            info.region = data.get("region")
                            info.is_resolved = True
                            await info.save()

                        await Player.filter(ip=ip).update(country=data.get("country"), region=data.get("region"))
                    except Exception as e:
                        logger.error(f"Error saving IP info for {ip}: {e}")
            for ip in server_ips:
                ping_val = await get_local_ping(ip)
                info = await IpInfo.get_or_none(ip=ip)
                if info:
                    info.ping = ping_val
                    info.is_resolved = True
                    await info.save()
                else:
                    await IpInfo.create(ip=ip, ping=ping_val, is_resolved=True)
        except Exception as e:
            logger.error(f"Error in ip_resolution_task: {e}")
        await asyncio.sleep(60)


async def sync_players_task():
    logger.info("Starting player sync background task...")
    target_keys = set(settings.r5_target_keys or [])
    rcon_key = settings.r5_rcon_key
    rcon_pwd = settings.r5_rcon_password
    servers_url = settings.r5_servers_url
    if not rcon_key or not rcon_pwd:
        logger.warning("RCON sync disabled because r5_rcon_key or r5_rcon_password is empty")
        return
    while True:
        try:
            server_list = None
            try:

                async def fetch_servers():
                    async with httpx.AsyncClient() as client:
                        response = await client.post(servers_url)
                        response.raise_for_status()
                        return response.json()["servers"]

                server_list = await fetch_servers()
            except Exception as e:
                logger.error(f"Failed to fetch server list: {e}")
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
                    current_server_cache = {}
                    online_nucleus_ids = set()
                    any_server_failed = False
                    for s in filtered_servers:
                        s_ip = s.get("ip")
                        try:
                            s_port = int(s.get("port"))
                        except (ValueError, TypeError):
                            continue
                        if not s_ip or not s_port:
                            continue
                        client = R5NetConsole(s_ip, s_port, rcon_key)
                        setattr(client, "rcon_password", rcon_pwd)
                        
                        try:
                            status_data = {}
                            proc_start = datetime.now()
                            await client.connect()
                            proc_duration = (datetime.now() - proc_start).total_seconds() * 1000
                            status_data["server_ping"] = int(proc_duration)

                            await client.authenticate_and_start(rcon_pwd)
                            status_data.update(await client.get_status())

                            players_data = status_data.get("players", [])
                            status_data["_server"] = f"{s_ip}:{s_port}"
                            status_data["_api_name"] = s.get("name", "Unknown Server")
                            
                            # Fallback/Merge max_players from API list if not parsed from RCON
                            if not status_data.get("max_players"):
                                try:
                                    status_data["max_players"] = int(s.get("maxPlayers", 0))
                                except (ValueError, TypeError):
                                    status_data["max_players"] = 0

                            full_name = status_data["_api_name"]
                            match = re.match(r"^(\[.*?\])", full_name)
                            status_data["short_name"] = match.group(1) if match else full_name
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

                                # Resolve IP
                                if p_data.get("ip"):
                                    ip_info_res = resolve_ip(p_data.get("ip"))
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
                            
                            
                        except Exception as e:
                            logger.error(f"Error syncing players from {client.host}: {e}")
                            any_server_failed = True
                        finally:
                            await client.close()
                        current_server_cache[f"{s_ip}:{s_port}"] = status_data
                    global_server_cache.clear()
                    global_server_cache.update(current_server_cache)
                    if not any_server_failed:
                        for p in all_players:
                            if p.nucleus_id and str(p.nucleus_id) not in online_nucleus_ids:
                                if p.status == "online":
                                    p.status = "offline"
                                    p.online_at = None
                                    await p.save()
                                elif p.status == "banned":
                                    pass
                                if str(p.nucleus_id) not in online_nucleus_ids:
                                    if p.status not in ("offline", "banned"):
                                        p.status = "offline"
                                        p.online_at = None
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
        await asyncio.sleep(10)
