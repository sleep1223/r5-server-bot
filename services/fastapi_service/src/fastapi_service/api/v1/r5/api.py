import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger
from shared_lib.config import settings
from shared_lib.models import BanRecord, Player, PlayerKilled
from tortoise.expressions import Q
from tortoise.functions import Count

from .cache import global_server_cache, raw_server_response_cache
from .netcon_client import R5NetConsole
from .utils import CN_TZ, get_date_range

security_scheme = HTTPBearer(auto_error=False)
ALLOWED_REASONS = ["NO_COVER", "BE_POLITE", "CHEAT", "RULES"]


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


@router.get("/server")
async def get_raw_server_list():
    """获取原始服务器列表缓存数据，无需鉴权"""
    return {"code": "0000", "data": raw_server_response_cache, "msg": "Raw server list retrieved"}


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
async def get_server_status(
    server_name: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
):
    """
    获取所有已连接服务器或特定服务器的状态。
    返回服务器名称（缩写）、在线玩家数量和 Ping。
    缩写规则：提取第一个 [] 内的内容，例如 "[CN(Shanghai)] PWLA..." -> "[CN(Shanghai)]"
    """
    # Check admin permission
    is_admin = False
    if not settings.fastapi_access_tokens:
        is_admin = True
    elif credentials and credentials.credentials in settings.fastapi_access_tokens:
        is_admin = True

    results = []

    # 从全局缓存中读取
    for server_cache in global_server_cache.values():
        try:
            # 获取主机名
            full_name = server_cache.get("_api_name") or server_cache.get("hostname") or server_cache.get("_server", "Unknown")

            # 如果请求，则进行过滤
            if server_name and server_name.lower() not in full_name.lower():
                continue

            # 从缓存获取数据
            short_name = server_cache.get("short_name")
            if not short_name:
                # Fallback if cache outdated
                match = re.match(r"^(\[.*?\])", full_name)
                short_name = match.group(1) if match else full_name

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

            # 获取玩家列表
            player_list = []
            for p in server_cache.get("players", []):
                p_info = {
                    "name": p.get("name", "Unknown"),
                }
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

    return {"code": "0000", "data": results, "msg": f"Server status for {len(results)} servers"}


@router.get("/players", dependencies=[Depends(verify_token)])
async def get_players(
    status: Literal["online", "offline", "banned", "kicked"] | None = "online",
    name: str | None = None,
    nucleus_id: int | None = None,
    country: str | None = None,
    region: str | None = None,
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
):
    query = Player.all()
    if status:
        query = query.filter(status=status)

    if name:
        query = query.filter(name__icontains=name)

    if nucleus_id:
        query = query.filter(nucleus_id=nucleus_id)

    if country:
        query = query.filter(country__icontains=country)

    if region:
        query = query.filter(region__icontains=region)

    total = await query.count()
    offset = (page_no - 1) * page_size
    players = await query.limit(page_size).offset(offset).values()
    return {"code": "0000", "data": players, "total": total, "msg": "Players retrieved"}


@router.get("/players/query")
async def query_player(
    q: int | str,
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=20, description="Items per page"),
):
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
    offset = (page_no - 1) * page_size
    players = await Player.filter(filter_q).offset(offset).limit(page_size)

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
                            # Cached metadata
                            "short_name": s_status.get("short_name"),
                            "country": s_status.get("country"),
                            "region": s_status.get("region"),
                            "server_ping": s_status.get("server_ping", 0),
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

            server_full_name = target_loc.get("server_name")

            # Short name fallback
            short_name = target_loc.get("short_name")
            if not short_name:
                match = re.match(r"^(\[.*?\])", server_full_name)
                short_name = match.group(1) if match else server_full_name

            server_info = {
                "name": server_full_name,
                "short_name": short_name,
                "host": target_loc.get("server_host"),
                "port": target_loc.get("server_port"),
                "ip": target_loc.get("server_host"),
                "country": target_loc.get("country"),
                "region": target_loc.get("region"),
                "ping": target_loc.get("server_ping"),
            }

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
            "player": {
                "name": player.name,
                "nucleus_id": player.nucleus_id,
                "status": player.status,
                "ban_count": player.ban_count,
                "kick_count": player.kick_count,
                "country": player.country,
                "region": player.region,
            },
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
async def kick_player(
    nucleus_id_or_player_name: int | str,
    reason: str = Query(..., description=f"Reason for kick. Allowed: {ALLOWED_REASONS}"),
):
    if reason not in ALLOWED_REASONS:
        raise HTTPException(status_code=400, detail=f"Invalid reason. Allowed: {ALLOWED_REASONS}")

    player_obj, error = await get_player_by_identifier(nucleus_id_or_player_name)
    if error:
        return error

    target_loc, error = get_online_location(player_obj)
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
        if await client.kick(player_obj.nucleus_id, f"KICK_REASON_{reason}"):
            success = True
    except Exception as e:
        logger.error(f"Failed to kick player on {target_host}:{target_port}: {e}")
    finally:
        await client.close()

    if success:
        if player_obj:
            # 改为原子锁+1
            await Player.filter(nucleus_id=player_obj.nucleus_id).update(kick_count=player_obj.kick_count + 1, status="kicked")
        return {"code": "0000", "data": None, "msg": f"Player {player_obj.nucleus_id} kicked from {target_loc['server_name']} for {reason}"}
    else:
        return {"code": "3000", "data": None, "msg": f"Failed to kick player {player_obj.nucleus_id}"}


@router.post("/players/{nucleus_id_or_player_name}/ban", dependencies=[Depends(verify_token)])
async def ban_player(
    nucleus_id_or_player_name: int | str,
    reason: str = Query(..., description=f"Reason for ban. Allowed: {ALLOWED_REASONS}"),
    credentials: HTTPAuthorizationCredentials | None = Depends(verify_token),
):
    if reason not in ALLOWED_REASONS:
        raise HTTPException(status_code=400, detail=f"Invalid reason. Allowed: {ALLOWED_REASONS}")

    player_obj, error = await get_player_by_identifier(nucleus_id_or_player_name)
    if error:
        return error

    target_loc, error = get_online_location(player_obj)
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
        if await client.bann(player_obj.nucleus_id, f"BAN_REASON_{reason}"):
            success = True
    except Exception as e:
        logger.error(f"Failed to ban player on {target_host}:{target_port}: {e}")
    finally:
        await client.close()

    if success:
        if player_obj:
            # Record ban
            operator_name = "admin" if credentials else "system"
            await BanRecord.create(player=player_obj, reason=reason, operator=operator_name)

            # 改为原子锁+1
            await Player.filter(nucleus_id=player_obj.nucleus_id).update(ban_count=player_obj.ban_count + 1, status="banned")
        return {"code": "0000", "data": None, "msg": f"Player {player_obj.nucleus_id} banned on {target_loc['server_name']} for {reason}"}
    else:
        return {"code": "3000", "data": None, "msg": f"Failed to ban player {player_obj.nucleus_id}"}


@router.post("/players/{nucleus_id_or_player_name}/unban", dependencies=[Depends(verify_token)])
async def unban_player(nucleus_id_or_player_name: int | str):
    player_obj, error = await get_player_by_identifier(nucleus_id_or_player_name)
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
                if await client.unban(player_obj.nucleus_id):
                    success = True
            finally:
                await client.close()
        except Exception as e:
            logger.error(f"Failed to unban on {server_key}: {e}")
            continue

    if success:
        if player_obj:
            player_obj.status = "offline"  # 或者其他默认值
            await player_obj.save()
        return {"code": "0000", "data": None, "msg": f"Player {player_obj.nucleus_id} unbanned"}
    else:
        # 如果没有任何成功，可能是因为没有在线服务器，或者都不成功
        if not global_server_cache:
            return {"code": "3000", "data": None, "msg": "No online servers found to execute unban"}
        return {"code": "3000", "data": None, "msg": f"Failed to unban player {player_obj.nucleus_id}"}


@router.get("/bans")
async def get_ban_list(
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
):
    """
    获取封禁记录列表。
    """
    # Check admin permission
    is_admin = False
    if not settings.fastapi_access_tokens:
        is_admin = True
    elif credentials and credentials.credentials in settings.fastapi_access_tokens:
        is_admin = True

    total = await BanRecord.all().count()
    offset = (page_no - 1) * page_size
    bans = await BanRecord.all().order_by("-created_at").offset(offset).limit(page_size).prefetch_related("player")

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
            player_info["player"].update({
                "region": ban.player.region,
            })
        results.append(player_info)

    return {"code": "0000", "data": results, "total": total, "msg": "Ban list retrieved"}


@router.get("/leaderboard/kd")
async def get_kd_leaderboard(
    range: Literal["today", "yesterday", "week", "month", "all"] = "all",
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
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
        return {"code": "0000", "data": [], "total": 0, "msg": f"KD Leaderboard for {range} range"}

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

    total = len(results)
    offset = (page_no - 1) * page_size
    paged_results = results[offset : offset + page_size]

    return {"code": "0000", "data": paged_results, "total": total, "msg": f"KD Leaderboard for {range} range"}


@router.get("/players/{nucleus_id_or_player_name}/vs_all")
async def get_player_vs_all_stats(
    nucleus_id_or_player_name: int | str,
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    sort: Literal["kills", "deaths", "kd"] = "kd",
):
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

    # 根据参数排序
    if sort == "kd":
        results.sort(key=lambda x: (x["kd"], x["kills"]), reverse=True)
    elif sort == "kills":
        results.sort(key=lambda x: (x["kills"], x["kd"]), reverse=True)
    elif sort == "deaths":
        results.sort(key=lambda x: (x["deaths"], x["kd"]), reverse=True)

    # Calculate Summary
    total_kills = len(kills_list)
    total_deaths = len(deaths_list)
    total_kd = 0.0
    if total_deaths > 0:
        total_kd = round(total_kills / total_deaths, 2)
    else:
        total_kd = float(total_kills)

    # Find Nemesis (宿敌) - High interaction, KD close to 1
    # Definition: 0.6 <= KD <= 1.6, Max (Kills + Deaths)
    nemesis = None
    max_interaction = 0

    # Find Worst Enemy (天敌) - Highest Enemy KD (Deaths / Kills)
    worst_enemy = None

    # Calculate Enemy KD for all results
    for r in results:
        k = r["kills"]
        d = r["deaths"]
        if k == 0:
            # If kills is 0, enemy kd is infinite. We use a large number + deaths for sorting
            r["enemy_kd"] = float(d) * 10000.0
            r["enemy_kd_display"] = float(d)  # For display purposes if needed, or just use d
        else:
            r["enemy_kd"] = round(d / k, 2)
            r["enemy_kd_display"] = r["enemy_kd"]

    # Sort by Enemy KD desc, then Deaths desc
    sorted_by_worst = sorted(results, key=lambda x: (x["enemy_kd"], x["deaths"]), reverse=True)

    # Try to find worst enemy with at least 5 deaths, then 2, then any
    candidates = [r for r in sorted_by_worst if r["deaths"] >= 5]
    if not candidates:
        candidates = [r for r in sorted_by_worst if r["deaths"] >= 2]
    if not candidates:
        candidates = sorted_by_worst

    if candidates:
        worst_enemy = candidates[0]
        # Ensure the dict has the enemy_kd value
        # Note: 'results' items are modified in place above, so they already have 'enemy_kd'

    # Logic for Nemesis
    for r in results:
        k = r["kills"]
        d = r["deaths"]
        kd = r["kd"]
        interaction = k + d

        # Check KD range (Close to 1)
        if 0.6 <= kd <= 1.66:
            if interaction > max_interaction:
                max_interaction = interaction
                nemesis = r

    summary = {"total_kills": total_kills, "total_deaths": total_deaths, "kd": total_kd, "nemesis": nemesis, "worst_enemy": worst_enemy}

    player_info = {"name": player.name, "nucleus_id": player.nucleus_id, "country": player.country, "region": player.region}

    total = len(results)
    offset = (page_no - 1) * page_size
    paged_results = results[offset : offset + page_size]

    return {"code": "0000", "data": paged_results, "total": total, "summary": summary, "player": player_info, "msg": f"KD Leaderboard for {nucleus_id_or_player_name}"}
