import asyncio
from datetime import datetime, timezone

import httpx
from loguru import logger
from shared_lib.config import settings
from shared_lib.models import Server

from fastapi_service.core.cache import server_cache
from fastapi_service.core.utils import parse_short_name


def _safe_int(val: object, default: int = 0) -> int:
    try:
        return int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


async def _upsert_servers_from_raw(raw_list: list[dict]) -> None:
    """把上游服务器列表同步到 Server 表，并翻转 has_status 标记。"""
    if not raw_list:
        return

    seen_hosts: set[str] = set()
    now = datetime.now(timezone.utc)

    for raw in raw_list:
        try:
            ip = str(raw.get("ip") or "").strip()
            if not ip:
                continue
            seen_hosts.add(ip)

            port = _safe_int(raw.get("port"), settings.ws_public_port or 37015)
            full_name = raw.get("name") or f"server-{ip}"
            short_name = parse_short_name(full_name) or None

            defaults = {
                "port": port,
                "name": full_name,
                "region": raw.get("region"),
                "netkey": raw.get("key"),
                "playlist": raw.get("playlist"),
                "map": raw.get("map"),
                "player_count": _safe_int(raw.get("playerCount") or raw.get("numPlayers")),
                "max_players": _safe_int(raw.get("maxPlayers")),
                "ping": _safe_int(raw.get("ping")),
                "has_status": True,
                "last_seen_at": now,
            }

            server, created = await Server.get_or_create(host=ip, defaults=defaults)
            if created:
                # 新行顺手补一个默认 short_name，避免用户必须手工设置后才能搜中文
                if short_name and not server.short_name:
                    server.short_name = short_name
                    await server.save(update_fields=["short_name", "updated_at"])
                continue

            # 已存在：更新 fetcher 拥有的字段，保留 short_name / is_self_hosted
            for field, value in defaults.items():
                setattr(server, field, value)
            await server.save(update_fields=[*defaults.keys(), "updated_at"])
        except Exception as e:
            logger.warning(f"upsert server row failed (ip={raw.get('ip')!r}): {e}")
            continue

    if seen_hosts:
        # 未在本次列表出现的活跃行翻 False
        await Server.filter(has_status=True).exclude(host__in=seen_hosts).update(has_status=False)


async def fetch_server_list_raw_task() -> None:
    """定时拉取远程服务器列表，并缓存到 server_cache.raw_response。

    拉取地址来源于 settings.r5_servers_url，拉取间隔来源于
    settings.r5_servers_fetch_interval（秒，默认 180s = 3 分钟）。
    """
    url = settings.r5_servers_url
    interval = max(int(settings.r5_servers_fetch_interval or 180), 1)
    logger.info(f"Raw server list fetch task started: url={url}, interval={interval}s")
    while True:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                raw_servers: list = []
                if isinstance(data, dict):
                    server_cache.update_raw_response(data)
                    maybe = data.get("servers")
                    if isinstance(maybe, list):
                        raw_servers = maybe
                elif isinstance(data, list):
                    server_cache.update_raw_response({"servers": data})
                    raw_servers = data

                raw_list = [s for s in raw_servers if isinstance(s, dict)]
                try:
                    await _upsert_servers_from_raw(raw_list)
                    logger.info(f"Raw server list fetched: {len(raw_list)} servers")
                except Exception as e:
                    logger.error(f"Error upserting Server rows: {e}")
            else:
                logger.warning(f"Failed to fetch raw server list: {response.status_code}")
        except Exception as e:
            logger.error(f"Error fetching raw server list: {e}")
        await asyncio.sleep(interval)
