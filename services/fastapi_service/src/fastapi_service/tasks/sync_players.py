import asyncio
import re
from datetime import datetime
from typing import Any

from loguru import logger
from shared_lib.config import settings
from shared_lib.models import IpInfo, Player, Server
from shared_lib.utils.ip import resolve_ip
from tortoise.exceptions import IntegrityError
from tortoise.expressions import F, Q
from utils.netcon_client import R5NetConsole

from fastapi_service.core.cache import server_cache
from fastapi_service.core.utils import CN_TZ, generate_hash

_SERVER_IDENTIFIER_FIELDS = ("serverId", "server_id", "key", "netkey")


async def _fetch_status_for_server(
    s: dict,
    s_ip: str,
    s_port: int,
    rcon_key: str,
    rcon_pwd: str,
    ip_info_map: dict[str, IpInfo],
) -> dict[str, Any]:
    """只做 RCON 连接 + 拉取 status，纯 IO，不动 DB。可并行调用。"""
    client = R5NetConsole(s_ip, s_port, rcon_key)
    try:
        proc_start = datetime.now()
        await client.connect()
        proc_duration = (datetime.now() - proc_start).total_seconds() * 1000

        authenticated = await client.authenticate_and_start(rcon_pwd)
        if not authenticated:
            raise RuntimeError("RCON 认证失败")

        status_data: dict[str, Any] = await client.get_status()
        if not status_data.get("players_parsed"):
            raw_status = str(status_data.get("raw") or "")
            raise RuntimeError(f"RCON status 解析失败(raw_len={len(raw_status)})")

        status_data["server_ping"] = int(proc_duration)
        status_data["_server"] = f"{s_ip}:{s_port}"
        status_data["_api_name"] = s.get("name", "未知服务器")
        status_data["server_id"] = _raw_server_identifier(s) or None

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
        return status_data
    finally:
        await client.close()


async def _process_status_players(
    status_data: dict[str, Any],
    all_players_index: dict[str, Player],
    online_nucleus_ids: set[str],
) -> None:
    """处理 status_data 中的玩家：使用 filter().update() 原子写库，避免跨任务的 read-modify-write 竞态。

    不再修改 ``all_players_index``；新建玩家创建后只放入索引以便后续查找，
    不参与外层 offline 检测（offline 检测用 DB 重新 query）。
    """
    players_data = status_data.get("players", []) or []
    now = datetime.now(CN_TZ)
    for p_data in players_data:
        r_nucleus_id = p_data.get("uniqueid")
        if not r_nucleus_id:
            continue
        r_nucleus_hash = generate_hash(r_nucleus_id)
        online_nucleus_ids.add(str(r_nucleus_id))

        raw_ping = p_data.get("ping", 0)
        real_ping = raw_ping // 2
        p_data["ping"] = real_ping

        update_dict: dict[str, Any] = {
            "nucleus_id": r_nucleus_id,
            "nucleus_hash": r_nucleus_hash,
            "ip": p_data.get("ip"),
            "ping": real_ping,
            "loss": p_data.get("loss", 0),
            "status": "online",
        }

        if p_data.get("ip"):
            ip_info_res = await asyncio.to_thread(resolve_ip, p_data.get("ip"))
            if ip_info_res:
                if ip_info_res.get("country"):
                    update_dict["country"] = ip_info_res["country"]
                    p_data["country"] = ip_info_res["country"]
                if ip_info_res.get("region"):
                    update_dict["region"] = ip_info_res["region"]
                    p_data["region"] = ip_info_res["region"]

        if p_data.get("name"):
            update_dict["name"] = p_data.get("name")

        matched_player = all_players_index.get(str(r_nucleus_id)) or all_players_index.get(r_nucleus_hash)

        if matched_player and matched_player.online_at:
            p_data["online_at"] = matched_player.online_at
        else:
            p_data["online_at"] = now

        if matched_player:
            if not p_data.get("country") and matched_player.country:
                p_data["country"] = matched_player.country
            if not p_data.get("region") and matched_player.region:
                p_data["region"] = matched_player.region

            if matched_player.status != "online" or not matched_player.online_at:
                update_dict["online_at"] = now
                p_data["online_at"] = now

            await Player.filter(id=matched_player.id).update(**update_dict)
        else:
            update_dict["online_at"] = now
            p_data["online_at"] = now
            try:
                new_player = await Player.create(**update_dict)
                all_players_index[str(r_nucleus_id)] = new_player
                all_players_index[r_nucleus_hash] = new_player
            except IntegrityError:
                existing_p = await Player.get_or_none(Q(nucleus_id=r_nucleus_id) | Q(nucleus_hash=r_nucleus_hash))
                if existing_p:
                    await Player.filter(id=existing_p.id).update(**update_dict)
                    all_players_index[str(r_nucleus_id)] = existing_p
                    all_players_index[r_nucleus_hash] = existing_p


async def _mark_offline_players(online_nucleus_ids: set[str]) -> None:
    """对未在 online_nucleus_ids 中的玩家原子地累加 playtime 并切 offline。

    使用 ``F('total_playtime_seconds') + session_seconds`` 做原子累加，
    与 ingest 进程的 ``_apply_player_disconnected`` 不会发生 lost-update。
    跳过 banned/kicked 状态以保留管理操作。
    """
    now = datetime.now(CN_TZ)
    candidates = await Player.filter(status="online").exclude(nucleus_id__isnull=True)
    for p in candidates:
        if p.nucleus_id and str(p.nucleus_id) in online_nucleus_ids:
            continue
        if p.status in ("banned", "kicked"):
            continue
        session_seconds = 0
        if p.online_at:
            session_seconds = max(0, int((now - p.online_at).total_seconds()))
        update_kwargs: dict[str, Any] = {"status": "offline", "online_at": None}
        if session_seconds > 0:
            update_kwargs["total_playtime_seconds"] = F("total_playtime_seconds") + session_seconds
        # 仅当当前仍是 online 时才覆盖（CAS），避免覆盖其它进程刚切到的状态
        await Player.filter(id=p.id, status="online").update(**update_kwargs)


def _build_player_index(players: list[Player]) -> dict[str, Player]:
    idx: dict[str, Player] = {}
    for p in players:
        if p.nucleus_id:
            idx[str(p.nucleus_id)] = p
        if p.nucleus_hash:
            idx[p.nucleus_hash] = p
    return idx


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _normalize_identifier(value: object) -> str:
    return str(value or "").strip()


def _raw_server_identifiers(server: dict) -> list[str]:
    identifiers: list[str] = []
    seen: set[str] = set()
    for field in _SERVER_IDENTIFIER_FIELDS:
        identifier = _normalize_identifier(server.get(field))
        if identifier and identifier not in seen:
            identifiers.append(identifier)
            seen.add(identifier)
    return identifiers


def _raw_server_identifier(server: dict) -> str:
    identifiers = _raw_server_identifiers(server)
    return identifiers[0] if identifiers else ""


def _is_cn_raw_server(server: dict) -> bool:
    name = str(server.get("name") or "")
    region = str(server.get("region") or "").upper()
    return "CN" in name or region in {"CN", "HK", "TW"}


def _has_rcon_endpoint(server: dict) -> bool:
    return bool(server.get("ip")) and _safe_int(server.get("port")) > 0


def _merge_db_endpoint(raw_server: dict, db_server: Server) -> dict:
    enriched = dict(raw_server)
    enriched["ip"] = db_server.host
    enriched["port"] = db_server.port

    if not enriched.get("name"):
        enriched["name"] = db_server.name
    if not enriched.get("region") and db_server.region:
        enriched["region"] = db_server.region
    if not enriched.get("serverId") and db_server.server_id:
        enriched["serverId"] = db_server.server_id
    if not enriched.get("key") and db_server.netkey:
        enriched["key"] = db_server.netkey
    if not enriched.get("playlist") and db_server.playlist:
        enriched["playlist"] = db_server.playlist
    if not enriched.get("map") and db_server.map:
        enriched["map"] = db_server.map
    if not enriched.get("numPlayers") and db_server.player_count:
        enriched["numPlayers"] = db_server.player_count
    if not enriched.get("maxPlayers") and db_server.max_players:
        enriched["maxPlayers"] = db_server.max_players
    return enriched


def _server_to_rcon_candidate(db_server: Server) -> dict:
    server_identifier = db_server.server_id or db_server.netkey or ""
    return {
        "name": db_server.name,
        "region": db_server.region,
        "serverId": server_identifier,
        "server_id": server_identifier,
        "key": db_server.netkey or server_identifier,
        "netkey": db_server.netkey or server_identifier,
        "ip": db_server.host,
        "port": db_server.port,
        "playlist": db_server.playlist,
        "map": db_server.map,
        "numPlayers": db_server.player_count,
        "maxPlayers": db_server.max_players,
    }


def _rcon_candidate_identity(server: dict) -> tuple[tuple[str, ...], str]:
    identifiers = tuple(_raw_server_identifiers(server))
    host = str(server.get("ip") or "").strip()
    port = _safe_int(server.get("port"))
    endpoint = f"{host}:{port}" if host and port else ""
    return identifiers, endpoint


async def _hydrate_rcon_endpoints_from_db(matched_servers: list[dict]) -> tuple[list[dict], int]:
    lookup_ids: set[str] = set()
    lookup_hosts: set[str] = set()
    for s in matched_servers:
        if _has_rcon_endpoint(s):
            continue
        lookup_ids.update(_raw_server_identifiers(s))
        host = str(s.get("ip") or "").strip()
        if host:
            lookup_hosts.add(host)

    if not lookup_ids and not lookup_hosts:
        return matched_servers, 0

    filters = []
    if lookup_ids:
        filters.append(Q(server_id__in=list(lookup_ids)) | Q(netkey__in=list(lookup_ids)))
    if lookup_hosts:
        filters.append(Q(host__in=list(lookup_hosts)))

    query = filters[0]
    for extra_filter in filters[1:]:
        query |= extra_filter

    db_servers = await Server.filter(query).all()
    by_identifier: dict[str, Server] = {}
    by_host: dict[str, Server] = {}
    for db_server in db_servers:
        if db_server.server_id:
            by_identifier[db_server.server_id] = db_server
        if db_server.netkey:
            by_identifier.setdefault(db_server.netkey, db_server)
        if db_server.host:
            by_host[db_server.host] = db_server

    hydrated_count = 0
    hydrated_servers: list[dict] = []
    for s in matched_servers:
        if _has_rcon_endpoint(s):
            hydrated_servers.append(s)
            continue

        db_server = None
        for server_identifier in _raw_server_identifiers(s):
            db_server = by_identifier.get(server_identifier)
            if db_server:
                break
        host = str(s.get("ip") or "").strip()
        if db_server is None and host:
            db_server = by_host.get(host)
        if db_server:
            hydrated_servers.append(_merge_db_endpoint(s, db_server))
            hydrated_count += 1
        else:
            hydrated_servers.append(s)

    return hydrated_servers, hydrated_count


async def _append_db_rcon_candidates(
    matched_servers: list[dict],
    target_keys: set[str],
) -> tuple[list[dict], int]:
    db_servers = await Server.filter(has_status=True).exclude(host__isnull=True).all()
    if not db_servers:
        return matched_servers, 0

    seen_identifiers: set[str] = set()
    seen_endpoints: set[str] = set()
    for server in matched_servers:
        identifiers, endpoint = _rcon_candidate_identity(server)
        seen_identifiers.update(identifiers)
        if endpoint:
            seen_endpoints.add(endpoint)

    appended = 0
    merged_servers = [*matched_servers]
    for db_server in db_servers:
        candidate = _server_to_rcon_candidate(db_server)
        if not _has_rcon_endpoint(candidate):
            continue

        identifiers, endpoint = _rcon_candidate_identity(candidate)
        identifier_set = set(identifiers)
        target_matched = bool(identifier_set & target_keys or db_server.host in target_keys or endpoint in target_keys)
        if target_keys:
            if not target_matched:
                continue
        elif not _is_cn_raw_server(candidate):
            continue
        if identifier_set and identifier_set & seen_identifiers:
            continue
        if endpoint and endpoint in seen_endpoints:
            continue

        merged_servers.append(candidate)
        seen_identifiers.update(identifier_set)
        if endpoint:
            seen_endpoints.add(endpoint)
        appended += 1

    return merged_servers, appended


def _collect_cached_online_nucleus_ids(server_keys: set[str]) -> set[str]:
    """从失败服务器的上一轮缓存中提取玩家 ID，避免误判为离线。"""
    cached_online_ids: set[str] = set()
    for server_key in server_keys:
        cached_status = server_cache.servers.get(server_key)
        if not cached_status:
            continue
        for p_data in cached_status.get("players", []) or []:
            r_nucleus_id = p_data.get("uniqueid")
            if r_nucleus_id:
                cached_online_ids.add(str(r_nucleus_id))
    return cached_online_ids


async def _run_one_cycle(
    filtered_servers: list[dict],
    rcon_key: str,
    rcon_pwd: str,
    ip_info_map: dict[str, IpInfo],
    per_server_timeout: float,
    max_concurrency: int,
) -> tuple[set[str], set[str], list[str], set[str]]:
    """并行抓取所有服务器状态，串行处理玩家更新。

    返回 (active_keys, online_nucleus_ids, failed_messages, failed_keys)。
    """
    targets: list[tuple[dict, str, int]] = []
    for s in filtered_servers:
        s_ip = s.get("ip")
        try:
            s_port = int(s.get("port") or 0)
        except (ValueError, TypeError):
            continue
        if not s_ip or not s_port:
            continue
        targets.append((s, s_ip, s_port))

    results: list[dict[str, Any] | Exception | None] = [None] * len(targets)
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def _fetch_target(idx: int, s: dict, s_ip: str, s_port: int) -> None:
        async with semaphore:
            try:
                results[idx] = await asyncio.wait_for(
                    _fetch_status_for_server(s, s_ip, s_port, rcon_key, rcon_pwd, ip_info_map),
                    timeout=per_server_timeout,
                )
            except Exception as exc:
                results[idx] = exc

    await asyncio.gather(*(asyncio.create_task(_fetch_target(idx, s, s_ip, s_port)) for idx, (s, s_ip, s_port) in enumerate(targets)))

    nucleus_ids: set[int] = set()
    nucleus_hashes: set[str] = set()
    for result in results:
        if result is None or isinstance(result, BaseException):
            continue
        for p_data in result.get("players", []) or []:
            r_nucleus_id = p_data.get("uniqueid")
            if not r_nucleus_id:
                continue
            r_nucleus_text = str(r_nucleus_id)
            if r_nucleus_text.isdigit():
                nucleus_ids.add(int(r_nucleus_text))
            nucleus_hashes.add(generate_hash(r_nucleus_text))

    player_filters = []
    if nucleus_ids:
        player_filters.append(Q(nucleus_id__in=list(nucleus_ids)))
    if nucleus_hashes:
        player_filters.append(Q(nucleus_hash__in=list(nucleus_hashes)))
    if player_filters:
        player_filter = player_filters[0]
        for extra_filter in player_filters[1:]:
            player_filter |= extra_filter
        all_players = await Player.filter(player_filter).all()
    else:
        all_players = []
    all_players_index = _build_player_index(all_players)

    active_keys: set[str] = set()
    online_nucleus_ids: set[str] = set()
    failed_servers: list[str] = []
    failed_keys: set[str] = set()

    # 串行处理玩家写库；RCON 慢但 DB 快，单进程串行写更安全
    for (s, s_ip, s_port), result in zip(targets, results):
        server_key = f"{s_ip}:{s_port}"
        if result is None:
            failed_keys.add(server_key)
            failed_servers.append(f"{server_key}(empty)")
            continue
        if isinstance(result, BaseException):
            failed_keys.add(server_key)
            if isinstance(result, asyncio.TimeoutError):
                failed_servers.append(f"{server_key}(timeout)")
            else:
                failed_servers.append(f"{server_key}({result})")
            continue
        try:
            await _process_status_players(result, all_players_index, online_nucleus_ids)
        except Exception as e:
            failed_keys.add(server_key)
            failed_servers.append(f"{server_key}(write:{e})")
            continue
        active_keys.add(server_key)
        server_cache.set_server(server_key, result)
        await asyncio.sleep(0)

    return active_keys, online_nucleus_ids, failed_servers, failed_keys


async def sync_players_task() -> None:
    logger.info("玩家同步后台任务已启动...")
    target_keys = {identifier for identifier in (_normalize_identifier(key) for key in settings.r5_target_keys or []) if identifier}
    rcon_key = settings.r5_rcon_key
    rcon_pwd = settings.r5_rcon_password
    if not rcon_key or not rcon_pwd:
        logger.warning("RCON 同步已禁用: r5_rcon_key 或 r5_rcon_password 为空")
        return

    configured_interval = float(getattr(settings, "r5_rcon_sync_interval", 180) or 180)
    sync_interval = max(configured_interval, 60.0)
    if sync_interval > configured_interval:
        logger.warning(f"r5_rcon_sync_interval={configured_interval:g}s 过低，RCON 轮询容易触发端口冷却；本次启动将使用 {sync_interval:g}s 以避免可用性抖动")

    while True:
        try:
            raw = server_cache.raw_response
            raw_servers = raw.get("servers") if isinstance(raw, dict) else None
            server_list = list(raw_servers) if isinstance(raw_servers, list) else None
            if not server_list:
                logger.debug("原始服务器列表缓存为空，等待 fetch_server_list_raw_task")
                await asyncio.sleep(2)
                continue

            matched_servers = []
            for s in server_list:
                if not isinstance(s, dict) or not _is_cn_raw_server(s):
                    continue
                identifiers = set(_raw_server_identifiers(s))
                if not target_keys or identifiers & target_keys:
                    matched_servers.append(s)

            matched_servers, hydrated_endpoint_count = await _hydrate_rcon_endpoints_from_db(matched_servers)
            if hydrated_endpoint_count:
                logger.info(f"RCON 同步已从本地 Server 记录补全 {hydrated_endpoint_count} 个服务器端点")
            matched_servers, appended_db_count = await _append_db_rcon_candidates(matched_servers, target_keys)
            if appended_db_count:
                logger.info(f"RCON 同步已追加 {appended_db_count} 个本地在线 Server 端点")

            filtered_servers = [s for s in matched_servers if _has_rcon_endpoint(s)]
            skipped_endpoint_count = len(matched_servers) - len(filtered_servers)
            if not filtered_servers:
                if matched_servers and skipped_endpoint_count:
                    logger.warning("RCON 同步跳过: 匹配到的服务器列表没有 ip/port 端点；主服务器仅提供原始服务器数量，且本地没有匹配的 Server host/port 记录")
                elif target_keys:
                    logger.warning("RCON 同步跳过: 没有 CN 服务器匹配 r5_target_keys (支持的标识字段: key/serverId/server_id/netkey)")
                else:
                    logger.warning("RCON 同步跳过: 原始服务器列表中没有 CN 服务器")
                await asyncio.sleep(sync_interval)
                continue
            if skipped_endpoint_count:
                logger.warning(f"RCON 同步将跳过 {skipped_endpoint_count}/{len(matched_servers)} 个缺少 ip/port 的匹配服务器")

            server_ips = [s.get("ip") for s in filtered_servers if s.get("ip")]
            ip_info_map: dict[str, IpInfo] = {}
            if server_ips:
                infos = await IpInfo.filter(ip__in=server_ips).all()
                for i in infos:
                    ip_info_map[i.ip] = i

            per_server_timeout = float(getattr(settings, "r5_rcon_per_server_timeout", 15))
            max_concurrency = int(getattr(settings, "r5_rcon_sync_concurrency", 8))
            sync_start = datetime.now()
            logger.info(f"开始同步玩家: {len(filtered_servers)} 个服务器 (并发={max_concurrency})")

            active_keys, online_nucleus_ids, failed_servers, failed_keys = await _run_one_cycle(filtered_servers, rcon_key, rcon_pwd, ip_info_map, per_server_timeout, max_concurrency)

            elapsed_ms = int((datetime.now() - sync_start).total_seconds() * 1000)
            if failed_servers:
                logger.warning(f"同步玩家: 成功 {len(active_keys)}/{len(filtered_servers)}, 耗时 {elapsed_ms}ms, 失败: {', '.join(failed_servers)}")
            else:
                logger.info(f"同步玩家: 成功 {len(active_keys)}/{len(filtered_servers)}, 耗时 {elapsed_ms}ms")

            cached_online_ids = _collect_cached_online_nucleus_ids(failed_keys)
            if cached_online_ids:
                online_nucleus_ids.update(cached_online_ids)
                logger.warning(f"为 {len(failed_keys)} 个失败服务器使用缓存玩家，避免 {len(cached_online_ids)} 名玩家被误判离线")

            server_cache.retain_servers(active_keys | failed_keys)
            await _mark_offline_players(online_nucleus_ids)
            if failed_servers:
                logger.warning("离线检测已用失败服务器的缓存数据完成")
        except asyncio.CancelledError:
            logger.info("玩家同步任务已取消")
            break
        except Exception as e:
            logger.error(f"sync_players_task 异常: {e}")
        await asyncio.sleep(sync_interval)
