import asyncio
import re
from datetime import datetime
from typing import Any

from loguru import logger
from shared_lib.config import settings
from shared_lib.models import IpInfo, Player
from shared_lib.utils.ip import resolve_ip
from tortoise.exceptions import IntegrityError
from tortoise.expressions import F, Q
from utils.netcon_client import R5NetConsole

from fastapi_service.core.cache import server_cache
from fastapi_service.core.utils import CN_TZ, generate_hash


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

        await client.authenticate_and_start(rcon_pwd)
        status_data: dict[str, Any] = await client.get_status()
        status_data["server_ping"] = int(proc_duration)
        status_data["_server"] = f"{s_ip}:{s_port}"
        status_data["_api_name"] = s.get("name", "Unknown Server")

        if not status_data.get("max_players"):
            try:
                status_data["max_players"] = int(s.get("maxPlayers", 0))
            except (ValueError, TypeError):
                status_data["max_players"] = 0

        full_name = status_data["_api_name"]
        match_complex = re.match(
            r"^\[(.*?)]\s*(.*?)\s*server QQ group\(\d+\)\s*(.*)$", full_name, re.IGNORECASE
        )
        if match_complex:
            status_data["short_name"] = (
                f"[{match_complex.group(1)}] {match_complex.group(2)} {match_complex.group(3)}".strip()
            )
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
                existing_p = await Player.get_or_none(
                    Q(nucleus_id=r_nucleus_id) | Q(nucleus_hash=r_nucleus_hash)
                )
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


async def _run_one_cycle(
    filtered_servers: list[dict],
    rcon_key: str,
    rcon_pwd: str,
    ip_info_map: dict[str, IpInfo],
    per_server_timeout: float,
    max_concurrency: int,
) -> tuple[set[str], set[str], list[str]]:
    """并行抓取所有服务器状态，串行处理玩家更新。返回 (active_keys, online_nucleus_ids, failed)。"""
    all_players = await Player.all()
    all_players_index = _build_player_index(all_players)

    sem = asyncio.Semaphore(max_concurrency)

    async def _bounded_fetch(s: dict, s_ip: str, s_port: int) -> dict[str, Any]:
        async with sem:
            return await asyncio.wait_for(
                _fetch_status_for_server(s, s_ip, s_port, rcon_key, rcon_pwd, ip_info_map),
                timeout=per_server_timeout,
            )

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

    fetch_tasks = [_bounded_fetch(s, ip, port) for s, ip, port in targets]
    results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    active_keys: set[str] = set()
    online_nucleus_ids: set[str] = set()
    failed_servers: list[str] = []

    # 串行处理玩家写库；RCON 慢但 DB 快，单进程串行写更安全
    for (s, s_ip, s_port), result in zip(targets, results):
        server_key = f"{s_ip}:{s_port}"
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.TimeoutError):
                failed_servers.append(f"{server_key}(timeout)")
            else:
                failed_servers.append(f"{server_key}({result})")
            continue
        try:
            await _process_status_players(result, all_players_index, online_nucleus_ids)
        except Exception as e:
            failed_servers.append(f"{server_key}(write:{e})")
            continue
        active_keys.add(server_key)
        server_cache.set_server(server_key, result)

    return active_keys, online_nucleus_ids, failed_servers


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
            raw = server_cache.raw_response
            raw_servers = raw.get("servers") if isinstance(raw, dict) else None
            server_list = list(raw_servers) if isinstance(raw_servers, list) else None
            if not server_list:
                logger.debug("Raw server list cache is empty, waiting for fetch_server_list_raw_task")
                await asyncio.sleep(2)
                continue

            filtered_servers = []
            for s in server_list:
                name = s.get("name", "")
                key = s.get("key", "")
                if "CN" in name and (not target_keys or key in target_keys):
                    filtered_servers.append(s)

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

            active_keys, online_nucleus_ids, failed_servers = await _run_one_cycle(
                filtered_servers, rcon_key, rcon_pwd, ip_info_map, per_server_timeout, max_concurrency
            )

            server_cache.retain_servers(active_keys)
            elapsed_ms = int((datetime.now() - sync_start).total_seconds() * 1000)
            if failed_servers:
                logger.warning(
                    f"同步玩家: 成功 {len(active_keys)}/{len(filtered_servers)}, 耗时 {elapsed_ms}ms, "
                    f"失败: {', '.join(failed_servers)}"
                )
            else:
                logger.info(
                    f"同步玩家: 成功 {len(active_keys)}/{len(filtered_servers)}, 耗时 {elapsed_ms}ms"
                )

            if not failed_servers:
                await _mark_offline_players(online_nucleus_ids)
            else:
                logger.warning("Skipping offline detection due to server sync failures")
        except asyncio.CancelledError:
            logger.info("Player sync task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in sync_players_task: {e}")
        await asyncio.sleep(settings.r5_rcon_sync_interval)
