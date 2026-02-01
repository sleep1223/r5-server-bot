import asyncio
import os
from datetime import datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from Cryptodome.Hash import SHA512
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger
from shared_lib.config import settings
from tortoise.expressions import Q
from tortoise.functions import Count

from .models import Player, PlayerKilled
from .netcon_client import R5NetConsole

# 全局服务器缓存: "host:port" -> server_status_dict (包含 players, _server 等信息)
global_server_cache: dict[str, dict] = {}

CN_TZ = ZoneInfo("Asia/Shanghai")


def generate_hash(data: str) -> str:
    hash_obj = SHA512.new(data.encode("utf-8"))
    return hash_obj.hexdigest()[:32]


async def sync_players_task():
    """定期从 RCON 获取状态并更新玩家数据库。"""
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
            # --- 服务器列表更新开始 ---
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
                    # 过滤服务器
                    filtered_servers = []
                    for s in server_list:
                        name = s.get("name", "")
                        key = s.get("key", "")
                        if "CN" in name and (not target_keys or key in target_keys):
                            filtered_servers.append(s)

                    # 预取所有玩家以优化匹配
                    all_players = await Player.all()

                    # 当前周期的临时缓存
                    current_server_cache = {}

                    # 跟踪当前周期的在线玩家以进行离线检测
                    online_nucleus_ids = set()

                    # 跟踪是否有服务器查询失败
                    any_server_failed = False

                    for s in filtered_servers:
                        s_ip = s.get("ip")
                        try:
                            s_port = int(s.get("port"))
                        except (ValueError, TypeError):
                            continue

                        if not s_ip or not s_port:
                            continue

                        # 新建
                        client = R5NetConsole(s_ip, s_port, rcon_key)
                        setattr(client, "rcon_password", rcon_pwd)

                        try:
                            # 连接 / 认证
                            await client.connect()
                            await client.authenticate_and_start(rcon_pwd)

                            # 同步玩家
                            status_data = await client.get_status()
                            players_data = status_data.get("players", [])

                            # 为状态数据添加元数据
                            status_data["_server"] = f"{s_ip}:{s_port}"
                            status_data["_api_name"] = s.get("name", "Unknown Server")

                            for p_data in players_data:
                                r_nucleus_id = p_data.get("uniqueid")
                                if not r_nucleus_id:
                                    continue

                                r_nucleus_hash = generate_hash(r_nucleus_id)
                                online_nucleus_ids.add(r_nucleus_id)

                                # 修正 ping 值 (除以 2)
                                raw_ping = p_data.get("ping", 0)
                                real_ping = raw_ping // 2
                                p_data["ping"] = real_ping

                                update_dict: dict[str, Any] = dict(
                                    nucleus_id=r_nucleus_id,
                                    nucleus_hash=r_nucleus_hash,
                                    ip=p_data.get("ip"),
                                    ping=real_ping,
                                    loss=p_data.get("loss", 0),
                                    status="online",
                                )

                                # 匹配玩家对象
                                matched_player = None
                                for p in all_players:
                                    if p.nucleus_id == r_nucleus_id or p.nucleus_hash == r_nucleus_hash:
                                        matched_player = p
                                        break

                                if p_data.get("name"):
                                    update_dict["name"] = p_data.get("name")

                                # 注入 online_at 到缓存数据中
                                if matched_player and matched_player.online_at:
                                    # 如果玩家已在线，使用数据库中的时间
                                    p_data["online_at"] = matched_player.online_at
                                else:
                                    # 否则认为是新上线
                                    p_data["online_at"] = datetime.now(CN_TZ)

                                if matched_player:
                                    if matched_player.status != "online" or not matched_player.online_at:
                                        update_dict["online_at"] = datetime.now(CN_TZ)
                                        # 也要更新缓存中的时间
                                        p_data["online_at"] = update_dict["online_at"]

                                    await matched_player.update_from_dict(update_dict).save()
                                else:
                                    # 创建新玩家
                                    update_dict["online_at"] = datetime.now(CN_TZ)
                                    p_data["online_at"] = update_dict["online_at"]
                                    new_player = await Player.create(**update_dict)
                                    logger.info(f"Created new player {new_player.name} ({new_player.nucleus_id})")
                                    all_players.append(new_player)

                            # 如果到达此处，说明同步成功
                            current_server_cache[f"{s_ip}:{s_port}"] = status_data

                        except Exception as e:
                            logger.error(f"Error syncing players from {client.host}: {e}")
                            any_server_failed = True
                        finally:
                            await client.close()

                    # 更新全局缓存
                    global_server_cache.clear()
                    global_server_cache.update(current_server_cache)

                    # 处理离线玩家
                    # 如果有任何服务器查询失败，我们跳过离线检测以避免错误地将玩家标记为离线
                    if not any_server_failed:
                        for p in all_players:
                            if p.nucleus_id and str(p.nucleus_id) not in online_nucleus_ids:
                                if p.status == "online":
                                    p.status = "offline"
                                    p.online_at = None
                                    await p.save()
                                elif p.status == "banned":
                                    pass  # 保持封禁状态

                                if str(p.nucleus_id) not in online_nucleus_ids:
                                    if p.status not in ("offline", "banned"):
                                        p.status = "offline"
                                        p.online_at = None
                                        await p.save()
                    else:
                        logger.warning("Skipping offline detection due to server sync failures")

                    logger.info(f"Player sync cycle completed. Active servers: {len(global_server_cache)}")

                except Exception as e:
                    logger.error(f"Error updating rcon clients: {e}")

        except asyncio.CancelledError:
            logger.info("Player sync task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in sync_players_task: {e}")

        await asyncio.sleep(10)


security_scheme = HTTPBearer(auto_error=False)


async def verify_token(credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme)):
    if not settings.fastapi_access_tokens:
        return
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials.credentials not in settings.fastapi_access_tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials


router = APIRouter()


@router.get("/server/info", dependencies=[Depends(verify_token)])
async def get_server_info():
    # 优先使用缓存
    if not global_server_cache:
        # 如果缓存为空，检查是否有连接
        pass

    # 直接返回缓存的值
    results = list(global_server_cache.values())

    return {"code": "0000", "data": results, "msg": "Server info retrieved"}


@router.get("/server/status")
async def get_server_status(server_name: str | None = None):
    """
    获取所有已连接服务器或特定服务器的状态。
    返回服务器名称（缩写）、在线玩家数量和 Ping。
    缩写规则：提取第一个 [] 内的内容，例如 "[CN(Shanghai)] PWLA..." -> "[CN(Shanghai)]"
    """
    results = []

    # 从全局缓存中读取
    for server_cache in global_server_cache.values():
        try:
            # 获取主机名
            full_name = server_cache.get("_api_name") or server_cache.get("hostname") or server_cache.get("_server", "Unknown")

            # 如果请求，则进行过滤
            if server_name and server_name.lower() not in full_name.lower():
                continue

            # 缩写名称
            # 规则："[CN(Shanghai)] PWLA 1v1 server..." -> "[CN(Shanghai)]"
            # 正则表达式以查找开头的 [...]
            import re

            match = re.match(r"^(\[.*?\])", full_name)
            short_name = match.group(1) if match else full_name

            player_count = len(server_cache.get("players", []))

            # 对于 ping，R5NetConsole 在我看到的片段中似乎没有明确测量它。
            # 我们将使用占位符，或者如果 `status` 有它。
            server_ping = 0  # 占位符

            # 获取玩家列表
            player_list = [p.get("name", "Unknown") for p in server_cache.get("players", [])]

            results.append({"name": short_name, "full_name": full_name, "player_count": player_count, "ping": server_ping, "host": server_cache.get("_server"), "players": player_list})

        except Exception as e:
            logger.error(f"Error processing status for {server_cache.get('_server')}: {e}")
            continue

    return {"code": "0000", "data": results, "msg": f"Server status for {len(results)} servers"}


@router.get("/players", dependencies=[Depends(verify_token)])
async def get_players(status: Literal["online", "offline", "banned", "kicked"] | None = "online", name: str | None = None, nucleus_id: int | None = None, limit: int = 100, offset: int = 0):
    query = Player.all()
    if status:
        query = query.filter(status=status)

    if name:
        query = query.filter(name__icontains=name)

    if nucleus_id:
        query = query.filter(nucleus_id=nucleus_id)

    total = await query.count()
    players = await query.limit(limit).offset(offset).values()
    return {"code": "0000", "data": players, "total": total, "msg": "Players retrieved"}


@router.get("/players/query")
async def query_player(q: int | str):
    """
    通过 nucleus_id (int/str) 或名称 (模糊搜索) 查询玩家。
    返回匹配的玩家列表及其在线状态。
    """
    # 1. 构建过滤器
    filter_q = Q(Q(nucleus_hash=q) | Q(name__icontains=q))
    if str(q).isdigit():
        # 如果 q 是数字，它可能是 nucleus_id 或名称的一部分（虽然名称纯数字的可能性较小）
        # 我们将其视为 nucleus_id 精确匹配 或 名称模糊匹配
        filter_q |= Q(nucleus_id=int(q))

    # 2. 获取玩家
    players = await Player.filter(filter_q).limit(20)  # 限制数量以避免结果过多

    if not players:
        return {"code": "4001", "data": [], "msg": f"No players found matching '{q}'"}

    results = []
    for player in players:
        # 检查缓存中的在线状态
        target_loc = None

        # 在 global_server_cache 中搜索
        if player.nucleus_id:
            p_id_str = str(player.nucleus_id)
            for s_status in global_server_cache.values():
                found_in_server = False
                for p_data in s_status.get("players", []):
                    if str(p_data.get("uniqueid")) == p_id_str:
                        # 解析主机和端口
                        host_port = s_status.get("_server", ":0").split(":")
                        host = host_port[0]
                        port = int(host_port[1]) if len(host_port) > 1 else 0

                        target_loc = {
                            "server_name": s_status.get("_api_name") or s_status.get("hostname"),
                            "server_host": host,
                            "server_port": port,
                            "online_at": p_data.get("online_at"),
                            "ping": p_data.get("ping", 0),
                        }
                        found_in_server = True
                        break
                if found_in_server:
                    break

        is_online = False
        duration = None
        server_info = None
        ping = 0

        if target_loc:
            is_online = True
            server_info = {"name": target_loc.get("server_name"), "host": target_loc.get("server_host"), "port": target_loc.get("server_port")}
            ping = target_loc.get("ping", 0)
            online_at = target_loc.get("online_at")
            if online_at:
                duration = (datetime.now(CN_TZ) - online_at).total_seconds()

        # 如果缓存中没有但数据库显示在线，则回退到数据库
        if not is_online and player.status == "online":
            is_online = True
            ping = player.ping
            if player.online_at:
                duration = (datetime.now(CN_TZ) - player.online_at).total_seconds()

        results.append({
            "is_online": is_online,
            "server": server_info,
            "duration_seconds": int(duration) if duration is not None else 0,
            "ping": ping,
            "player": {"name": player.name, "nucleus_id": player.nucleus_id, "status": player.status, "ban_count": player.ban_count, "kick_count": player.kick_count},
        })

    return {"code": "0000", "data": results, "msg": f"Found {len(results)} players"}


async def get_player_by_identifier(identifier: int | str, require_nucleus_id: bool = True) -> tuple[Player | None, dict | None]:
    filter_q = Q(Q(nucleus_hash=identifier) | Q(name__iexact=identifier))
    if isinstance(identifier, int):
        filter_q |= Q(nucleus_id=identifier)

    player = await Player.filter(filter_q).first()
    if not player:
        return None, {"code": "4001", "data": None, "msg": f"Player {identifier} not found"}

    if require_nucleus_id and not player.nucleus_id:
        return None, {"code": "4002", "data": None, "msg": f"Player {identifier} has no nucleus_id"}

    return player, None


def get_online_location(player: Player) -> tuple[dict | None, dict | None]:
    p_id_str = str(player.nucleus_id)

    for s_status in global_server_cache.values():
        for p_data in s_status.get("players", []):
            if str(p_data.get("uniqueid")) == p_id_str:
                # 解析主机和端口
                host_port = s_status.get("_server", ":0").split(":")
                host = host_port[0]
                port = int(host_port[1]) if len(host_port) > 1 else 0

                target_loc = {"server_name": s_status.get("_api_name") or s_status.get("hostname"), "server_host": host, "server_port": port, "online_at": p_data.get("online_at")}
                return target_loc, None

    return None, {"code": "4004", "data": None, "msg": f"Player {player.name} is not online"}


@router.post("/players/{nucleus_id_or_player_name}/kick", dependencies=[Depends(verify_token)])
async def kick_player(nucleus_id_or_player_name: int | str):
    player, error = await get_player_by_identifier(nucleus_id_or_player_name)
    if error:
        return error

    target_loc, error = get_online_location(player)
    if error:
        return error

    success = False

    # 目标特定服务器
    target_host = target_loc["server_host"]
    target_port = target_loc["server_port"]

    # 临时连接
    rcon_key = settings.r5_rcon_key
    rcon_pwd = settings.r5_rcon_password
    if not rcon_key or not rcon_pwd:
        raise HTTPException(status_code=503, detail="RCON configuration missing")

    client = R5NetConsole(target_host, target_port, rcon_key)
    try:
        await client.connect()
        await client.authenticate_and_start(rcon_pwd)
        if await client.kick(player.nucleus_id):
            success = True
    except Exception as e:
        logger.error(f"Failed to kick player on {target_host}:{target_port}: {e}")
    finally:
        await client.close()

    if success:
        if player:
            player.kick_count += 1
            player.status = "kicked"
            await player.save()
        return {"code": "0000", "data": None, "msg": f"Player {player.nucleus_id} kicked from {target_loc['server_name']}"}
    else:
        return {"code": "3000", "data": None, "msg": f"Failed to kick player {player.nucleus_id}"}


@router.post("/players/{nucleus_id_or_player_name}/ban", dependencies=[Depends(verify_token)])
async def ban_player(nucleus_id_or_player_name: int | str):
    player, error = await get_player_by_identifier(nucleus_id_or_player_name)
    if error:
        return error

    target_loc, error = get_online_location(player)
    if error:
        return error

    success = False

    # 目标特定服务器
    target_host = target_loc["server_host"]
    target_port = target_loc["server_port"]

    # 临时连接
    rcon_key = settings.r5_rcon_key
    rcon_pwd = settings.r5_rcon_password
    if not rcon_key or not rcon_pwd:
        raise HTTPException(status_code=503, detail="RCON configuration missing")

    client = R5NetConsole(target_host, target_port, rcon_key)
    try:
        await client.connect()
        await client.authenticate_and_start(rcon_pwd)
        if await client.ban(player.nucleus_id):
            success = True
    except Exception as e:
        logger.error(f"Failed to ban player on {target_host}:{target_port}: {e}")
    finally:
        await client.close()

    if success:
        if player:
            player.ban_count += 1
            player.status = "banned"
            await player.save()
        return {"code": "0000", "data": None, "msg": f"Player {player.nucleus_id} banned on {target_loc['server_name']}"}
    else:
        return {"code": "3000", "data": None, "msg": f"Failed to ban player {player.nucleus_id}"}


@router.post("/players/{nucleus_id_or_player_name}/unban", dependencies=[Depends(verify_token)])
async def unban_player(nucleus_id_or_player_name: int | str):
    player, error = await get_player_by_identifier(nucleus_id_or_player_name)
    if error:
        return error

    rcon_key = settings.r5_rcon_key
    rcon_pwd = settings.r5_rcon_password
    if not rcon_key or not rcon_pwd:
        raise HTTPException(status_code=503, detail="RCON configuration missing")

    success = False

    # 尝试在所有已知服务器上解封
    # 使用 global_server_cache 中的服务器列表
    for server_key in global_server_cache.keys():
        try:
            host, port = server_key.split(":")
            port = int(port)

            client = R5NetConsole(host, port, rcon_key)
            try:
                await client.connect()
                await client.authenticate_and_start(rcon_pwd)
                if await client.unban(player.nucleus_id):
                    success = True
            finally:
                await client.close()
        except Exception as e:
            logger.error(f"Failed to unban on {server_key}: {e}")
            continue

    if success:
        if player:
            player.status = "offline"  # 或者其他默认值
            await player.save()
        return {"code": "0000", "data": None, "msg": f"Player {player.nucleus_id} unbanned"}
    else:
        # 如果没有任何成功，可能是因为没有在线服务器，或者都不成功
        if not global_server_cache:
            return {"code": "3000", "data": None, "msg": "No online servers found to execute unban"}
        return {"code": "3000", "data": None, "msg": f"Failed to unban player {player.nucleus_id}"}


def get_date_range(range_type: str):
    now = datetime.now(CN_TZ)
    start_time = None
    end_time = None

    if range_type == "today":
        start_time = datetime.combine(now.date(), time.min, tzinfo=CN_TZ)
        end_time = now
    elif range_type == "yesterday":
        yesterday = now - timedelta(days=1)
        start_time = datetime.combine(yesterday.date(), time.min, tzinfo=CN_TZ)
        end_time = datetime.combine(yesterday.date(), time.max, tzinfo=CN_TZ)
    elif range_type == "week":
        # 周一作为一周的开始
        start_of_week = now.date() - timedelta(days=now.weekday())
        start_time = datetime.combine(start_of_week, time.min, tzinfo=CN_TZ)
        end_time = now
    elif range_type == "month":
        start_time = datetime.combine(now.date().replace(day=1), time.min, tzinfo=CN_TZ)
        end_time = now

    return start_time, end_time


@router.get("/leaderboard/kd")
async def get_kd_leaderboard(
    range: Literal["today", "yesterday", "week", "month", "all"] = "all",
    limit: int = 20,
    sort: Literal["kills", "deaths", "kd"] = "kd",
    min_kills: int = 100,
    min_deaths: int = 0,
):
    """
    获取全局 KD 排行榜。
    范围选项：today, yesterday, week, month, all (默认)
    """
    start_time, end_time = get_date_range(range)

    # 基础过滤器
    filters = {}
    if start_time:
        filters["created_at__gte"] = start_time
    if end_time:
        filters["created_at__lte"] = end_time

    kills_qs = PlayerKilled.filter(**filters, attacker_id__isnull=False)
    kills_data = await kills_qs.group_by("attacker_id").annotate(k_count=Count("id")).values("attacker_id", "k_count")

    # 聚合死亡数：按受害者分组
    deaths_qs = PlayerKilled.filter(**filters, victim_id__isnull=False)
    deaths_data = await deaths_qs.group_by("victim_id").annotate(d_count=Count("id")).values("victim_id", "d_count")

    # 在内存中合并统计数据
    stats = {}

    for k in kills_data:
        pid = k["attacker_id"]
        if pid not in stats:
            stats[pid] = {"kills": 0, "deaths": 0}
        stats[pid]["kills"] = k["k_count"]

    for d in deaths_data:
        pid = d["victim_id"]
        if pid not in stats:
            stats[pid] = {"kills": 0, "deaths": 0}
        stats[pid]["deaths"] = d["d_count"]

    if not stats:
        return []

    # 获取玩家详细信息
    player_ids = list(stats.keys())
    players = await Player.filter(id__in=player_ids).values("id", "name", "nucleus_id")
    p_map = {p["id"]: p for p in players}

    results = []
    for pid, data in stats.items():
        p_info = p_map.get(pid)
        # 如果玩家已删除或未找到，跳过或显示未知
        name = p_info["name"] if p_info else f"Unknown ({pid})"
        nucleus_id = p_info["nucleus_id"] if p_info else None

        kills = data["kills"]
        deaths = data["deaths"]

        # 计算 KD
        if deaths == 0:
            kd = float(kills)
        else:
            kd = round(kills / deaths, 2)

        # 应用过滤器
        if kills < min_kills or deaths < min_deaths:
            continue

        results.append({"name": name, "nucleus_id": nucleus_id, "kills": kills, "deaths": deaths, "kd": kd})

        # 排序：主要 KD (降序)，次要击杀数 (降序)
        if sort == "kd":
            results.sort(key=lambda x: (x["kd"], x["kills"]), reverse=True)
        elif sort == "kills":
            results.sort(key=lambda x: x["kills"], reverse=True)
        elif sort == "deaths":
            results.sort(key=lambda x: x["deaths"], reverse=True)

    return {"code": "0000", "data": results[:limit], "msg": f"KD Leaderboard for {range} range"}


@router.get("/players/{nucleus_id_or_player_name}/vs_all")
async def get_player_vs_all_stats(nucleus_id_or_player_name: int | str):
    """
    获取特定玩家对其他所有人的 KD（从高到低）。
    """

    player, error = await get_player_by_identifier(nucleus_id_or_player_name, require_nucleus_id=False)
    if error:
        return error

    pid = player.id

    # 1. 获取该玩家的所有击杀 (玩家 -> 受害者)
    kills_list = await PlayerKilled.filter(attacker_id=pid, victim_id__not_isnull=True).values("victim_id")

    # 2. 获取该玩家的所有死亡 (攻击者 -> 玩家)
    deaths_list = await PlayerKilled.filter(victim_id=pid, attacker_id__not_isnull=True).values("attacker_id")

    opponents_stats = {}

    # 处理击杀
    for k in kills_list:
        oid = k["victim_id"]
        if oid not in opponents_stats:
            opponents_stats[oid] = {"kills": 0, "deaths": 0}
        opponents_stats[oid]["kills"] += 1

    # 处理死亡
    for d in deaths_list:
        oid = d["attacker_id"]
        if oid not in opponents_stats:
            opponents_stats[oid] = {"kills": 0, "deaths": 0}
        opponents_stats[oid]["deaths"] += 1

    if not opponents_stats:
        return {"code": "0000", "data": [], "msg": f"Player {nucleus_id_or_player_name} has no opponents"}

    # 获取对手详细信息
    op_ids = list(opponents_stats.keys())
    ops = await Player.filter(id__in=op_ids).values("id", "name", "nucleus_id")
    op_map = {o["id"]: o for o in ops}

    results = []
    for oid, data in opponents_stats.items():
        op_info = op_map.get(oid)
        name = op_info["name"] if op_info else f"Unknown ({oid})"
        n_id = op_info["nucleus_id"] if op_info else None

        k = data["kills"]
        d = data["deaths"]

        if d == 0:
            kd = float(k)
        else:
            kd = round(k / d, 2)

        results.append({"opponent_name": name, "opponent_id": n_id, "kills": k, "deaths": d, "kd": kd})

    # 按 KD 降序排序
    results.sort(key=lambda x: (x["kd"], x["kills"]), reverse=True)

    return {"code": "0000", "data": results, "msg": f"KD Leaderboard for {nucleus_id_or_player_name}"}
