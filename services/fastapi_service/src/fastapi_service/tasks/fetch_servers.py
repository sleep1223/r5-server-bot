import asyncio
from datetime import datetime, timezone
from typing import Any

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


def _raw_server_identifier(raw: dict) -> str:
    for field in ("serverId", "server_id", "key", "netkey"):
        value = raw.get(field)
        if value:
            return str(value)
    return ""


def _raw_server_netkey(raw: dict) -> str | None:
    for field in ("key", "netkey"):
        value = str(raw.get(field) or "").strip()
        if value:
            return value
    return None


def _is_cn_region(raw: dict) -> bool:
    return str(raw.get("region") or "").strip().upper() == "CN"


async def _upsert_servers_from_raw(raw_list: list[dict]) -> None:
    """把上游服务器列表同步到 Server 表，并翻转 has_status 标记。"""
    if not raw_list:
        return

    seen_hosts: set[str] = set()
    seen_server_ids: set[str] = set()
    now = datetime.now(timezone.utc)

    for raw in raw_list:
        try:
            ip = str(raw.get("ip") or "").strip()
            server_identifier = _raw_server_identifier(raw)
            if not ip and not server_identifier:
                continue

            port = _safe_int(raw.get("port"), settings.ws_public_port or 37015)
            full_name = raw.get("name") or f"server-{ip or server_identifier}"
            short_name = parse_short_name(full_name) or None
            raw_netkey = _raw_server_netkey(raw)

            server = await Server.get_or_none(server_id=server_identifier) if server_identifier else None
            if server is None and server_identifier and _is_cn_region(raw):
                server = (
                    await Server.filter(name=full_name)
                    .exclude(server_id=server_identifier)
                    .exclude(server_id__isnull=True)
                    .order_by("-last_seen_at", "-id")
                    .first()
                )
                if server:
                    logger.info(
                        f"CN 服务器 server_id 已按名称更新: "
                        f"name={full_name}, old={server.server_id}, new={server_identifier}"
                    )
            if not ip and server is None:
                logger.debug(f"跳过缺少 host 的原始服务器: server_id={server_identifier}")
                continue

            if ip:
                seen_hosts.add(ip)
            if server_identifier:
                seen_server_ids.add(server_identifier)

            defaults = {
                "port": port,
                "name": full_name,
                "region": raw.get("region"),
                "playlist": raw.get("playlist"),
                "map": raw.get("map"),
                "player_count": _safe_int(raw.get("playerCount") or raw.get("numPlayers")),
                "max_players": _safe_int(raw.get("maxPlayers")),
                "ping": _safe_int(raw.get("ping")),
                "has_status": True,
                "last_seen_at": now,
            }
            if raw_netkey:
                defaults["netkey"] = raw_netkey
            if server_identifier:
                defaults["server_id"] = server_identifier

            if server is None and ip:
                server = await Server.get_or_none(host=ip)

            created = False
            if server is None:
                server = await Server.create(host=ip, **defaults)
                created = True
            elif ip and server.host != ip:
                conflict = await Server.get_or_none(host=ip)
                if conflict and conflict.id != server.id:
                    if server_identifier and conflict.server_id not in (None, server_identifier):
                        logger.warning(f"合并原始服务器行时 server_id 冲突: server_id={server_identifier}, host={ip}, conflict_id={conflict.id}")
                        continue
                    await Server.filter(id=server.id).update(server_id=None, has_status=False)
                    server = conflict
                else:
                    server.host = ip

            if created:
                # 新行顺手补一个默认 short_name，避免用户必须手工设置后才能搜中文
                if short_name and not server.short_name:
                    server.short_name = short_name
                    await server.save(update_fields=["short_name", "updated_at"])
                continue

            # 已存在：更新 fetcher 拥有的字段，保留 short_name / is_self_hosted
            for field, value in defaults.items():
                setattr(server, field, value)
            update_fields = [*defaults.keys(), "updated_at"]
            if ip:
                update_fields.append("host")
            await server.save(update_fields=update_fields)
        except Exception as e:
            logger.warning(f"写入 Server 行失败(ip={raw.get('ip')!r}, server_id={raw.get('serverId')!r}): {e}")
            continue

    if seen_hosts or seen_server_ids:
        # 未在本次列表出现的活跃行翻 False
        query = Server.filter(has_status=True)
        if seen_server_ids and not seen_hosts:
            query = query.filter(server_id__isnull=False)
        elif seen_hosts and not seen_server_ids:
            query = query.filter(host__isnull=False)
        if seen_hosts:
            query = query.exclude(host__in=seen_hosts)
        if seen_server_ids:
            query = query.exclude(server_id__in=seen_server_ids)
        await query.update(has_status=False)


async def fetch_server_list_raw_once() -> int | None:
    """Fetch the remote server list once and update cache/database rows."""
    url = settings.r5_servers_url
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, timeout=10.0)
        if response.status_code != 200:
            logger.warning(f"拉取原始服务器列表失败: {response.status_code}")
            return None

        data = response.json()
        raw_servers: list[Any] = []
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
            logger.info(f"原始服务器列表已拉取: {len(raw_list)} 台服务器")
        except Exception as e:
            logger.error(f"写入 Server 行异常: {e}")
            return None
        return len(raw_list)
    except Exception as e:
        logger.error(f"拉取原始服务器列表异常: {e}")
        return None


async def fetch_server_list_raw_task(*, delay_first: bool = False) -> None:
    """定时拉取远程服务器列表，并缓存到 server_cache.raw_response。

    拉取地址来源于 settings.r5_servers_url，拉取间隔来源于
    settings.r5_servers_fetch_interval（秒，默认 180s = 3 分钟）。
    """
    url = settings.r5_servers_url
    interval = max(int(settings.r5_servers_fetch_interval or 180), 1)
    logger.info(f"原始服务器列表拉取任务已启动: url={url}, interval={interval}s")
    if delay_first:
        await asyncio.sleep(interval)
    while True:
        await fetch_server_list_raw_once()
        await asyncio.sleep(interval)
