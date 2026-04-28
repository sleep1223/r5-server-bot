import asyncio

from loguru import logger
from shared_lib.config import settings
from shared_lib.models import BanRecord, Player
from tortoise.expressions import F

from fastapi_service.core.cache import server_cache

from .rcon import rcon_session


async def broadcast_kick_player(
    nucleus_id: int,
    reason: str,
    servers: list[dict],
    rcon_key: str,
    rcon_pwd: str,
    *,
    per_server_timeout: float = 3.0,
) -> tuple[int, dict | None]:
    """在所有给定服务器并行 kickid，避免依赖可能过期的玩家位置缓存。

    返回 (success_count, hit_server)。kick 仅在玩家所在服返回成功，
    success_count 最多为 1。
    """
    if not servers:
        return 0, None

    async def _kick_one(s: dict) -> tuple[dict, bool]:
        try:
            async with rcon_session(s["server_host"], s["server_port"], rcon_key, rcon_pwd, timeout=per_server_timeout) as client:
                ok = await client.kick(nucleus_id, f"#KICK_REASON_{reason}")
                return s, ok
        except Exception as e:
            logger.warning(f"Broadcast kick failed on {s.get('server_key')}: {e}")
            return s, False

    results = await asyncio.gather(*[_kick_one(s) for s in servers])
    hit = next((s for s, ok in results if ok), None)
    return sum(1 for _, ok in results if ok), hit


async def broadcast_bann_player(
    nucleus_id: int,
    reason: str,
    servers: list[dict],
    rcon_key: str,
    rcon_pwd: str,
    *,
    per_server_timeout: float = 3.0,
    online_server_key: str | None = None,
) -> tuple[int, list[dict]]:
    """在所有给定服务器并行封禁。

    - 玩家所在服 (online_server_key) 使用 banid (= kickid + banid),要求玩家在线;
    - 其他服使用 bannid,仅加入封禁名单。
    若未指定 online_server_key,所有服一律走 bannid。
    若指定了 online_server_key 但该服不在 servers 中(缓存过期或被过滤),
    则所有服走 bannid,并额外对每台服调用一次 kickid 以确保玩家被踢出。

    返回 (success_count, hit_servers)。
    """
    if not servers:
        return 0, []

    online_server_present = online_server_key is not None and any(s.get("server_key") == online_server_key for s in servers)
    need_extra_kick = online_server_key is not None and not online_server_present

    async def _ban_one(s: dict) -> tuple[dict, bool]:
        is_online_server = online_server_key is not None and s.get("server_key") == online_server_key
        try:
            async with rcon_session(s["server_host"], s["server_port"], rcon_key, rcon_pwd, timeout=per_server_timeout) as client:
                if is_online_server:
                    ok = await client.ban(nucleus_id, f"#BAN_REASON_{reason}")
                    if not ok:
                        logger.warning(f"ban failed on online server {s.get('server_key')}, falling back to bann+kick")
                        ok = await client.bann(nucleus_id, f"#BAN_REASON_{reason}")
                        await client.kick(nucleus_id, f"#KICK_REASON_{reason}")
                else:
                    ok = await client.bann(nucleus_id, f"#BAN_REASON_{reason}")
                    if need_extra_kick:
                        await client.kick(nucleus_id, f"#KICK_REASON_{reason}")
                return s, ok
        except Exception as e:
            logger.warning(f"Broadcast ban failed on {s.get('server_key')}: {e}")
            return s, False

    results = await asyncio.gather(*[_ban_one(s) for s in servers])
    return sum(1 for _, ok in results if ok), [s for s, ok in results if ok]


async def unban_player_on_server(nucleus_id: int, host: str, port: int, rcon_key: str, rcon_pwd: str, *, timeout: float = 10.0) -> bool:
    try:
        async with rcon_session(host, port, rcon_key, rcon_pwd, timeout=timeout) as client:
            return await client.unban(nucleus_id)
    except Exception as e:
        logger.error(f"Failed to unban player on {host}:{port}: {e}")
        return False


async def record_ban(player: Player, reason: str, operator_name: str) -> None:
    await BanRecord.create(player=player, reason=reason, operator=operator_name)
    await Player.filter(id=player.id).update(ban_count=F("ban_count") + 1, status="banned")


async def record_kick(player: Player) -> None:
    await Player.filter(id=player.id).update(kick_count=F("kick_count") + 1, status="kicked")


async def record_kick_offline(player: Player) -> None:
    await Player.filter(id=player.id).update(kick_count=F("kick_count") + 1)


async def mark_status_kicked(player: Player) -> None:
    await Player.filter(id=player.id).update(status="kicked")


async def record_unban(nucleus_id: int) -> None:
    await Player.filter(nucleus_id=nucleus_id).update(status="offline")
    server_cache.clear_ban_location(nucleus_id)


# ── Background tasks ──

# 持有 fire-and-forget Task 的强引用，避免被 GC。done 后自动 discard。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


async def run_unban_on_servers_background(
    *,
    player_id: int,
    nucleus_id: int,
    servers: list[dict],
) -> None:
    rcon_key = settings.r5_rcon_key
    rcon_pwd = settings.r5_rcon_password
    if not rcon_key or not rcon_pwd:
        logger.error("Background unban aborted: RCON configuration missing")
        return

    success_count = 0

    for server in servers:
        host = server["server_host"]
        port = server["server_port"]
        server_key = server["server_key"]
        try:
            async with rcon_session(host, port, rcon_key, rcon_pwd) as client:
                if await client.unban(nucleus_id):
                    success_count += 1
        except Exception as e:
            logger.error(f"Failed to unban player on {server_key}: {e}")

    if success_count > 0:
        await Player.filter(id=player_id).update(status="offline")
        server_cache.clear_ban_location(nucleus_id)

    logger.info(f"Background unban task finished for {nucleus_id}. success={success_count}/{len(servers)}")


def schedule_unban_background(*, player_id: int, nucleus_id: int, servers: list[dict]) -> None:
    task = asyncio.create_task(
        run_unban_on_servers_background(
            player_id=player_id,
            nucleus_id=nucleus_id,
            servers=servers,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
