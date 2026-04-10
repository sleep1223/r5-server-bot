import asyncio

from loguru import logger
from shared_lib.models import IpInfo, Player

from fastapi_service.core.cache import server_cache
from fastapi_service.core.utils import get_local_ping, resolve_ips_batch


async def ip_resolution_task() -> None:
    while True:
        try:
            server_ips = set()
            ips_to_process = set()
            for server_data in server_cache.servers.values():
                host_str = server_data.get("_server")
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
            players_by_ip: dict[str, list] = {}
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
                            info.country = data.get("country") or ""
                            info.region = data.get("region") or ""
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
