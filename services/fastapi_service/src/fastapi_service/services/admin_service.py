import asyncio
from typing import Any

from loguru import logger
from shared_lib.config import settings
from shared_lib.models import BanRecord, Player, PlayerAccessNotice, PlayerAccessOperation
from tortoise.expressions import F, Q

from fastapi_service.core.cache import server_cache
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error

from . import player_access_service
from .rcon import rcon_session

SELF_UNBAN_CONFIRMATION_TEXT = "我已了解规则"


def _normalize_exact_target(value: object | None) -> str:
    return str(value or "").strip().casefold()


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
                ok = await client.kick(nucleus_id, player_access_service.action_reason_text("kick", reason))
                return s, ok
        except Exception as e:
            logger.warning(f"广播踢出失败: server={s.get('server_key')}, error={e}")
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
                    ok = await client.ban(nucleus_id, player_access_service.action_reason_text("ban", reason))
                    if not ok:
                        logger.warning(f"在线服务器封禁失败，回退到 bann+kick: server={s.get('server_key')}")
                        ok = await client.bann(nucleus_id, player_access_service.action_reason_text("ban", reason))
                        await client.kick(nucleus_id, player_access_service.action_reason_text("kick", reason))
                else:
                    ok = await client.bann(nucleus_id, player_access_service.action_reason_text("ban", reason))
                    if need_extra_kick:
                        await client.kick(nucleus_id, player_access_service.action_reason_text("kick", reason))
                return s, ok
        except Exception as e:
            logger.warning(f"广播封禁失败: server={s.get('server_key')}, error={e}")
            return s, False

    results = await asyncio.gather(*[_ban_one(s) for s in servers])
    return sum(1 for _, ok in results if ok), [s for s, ok in results if ok]


async def unban_player_on_server(nucleus_id: int, host: str, port: int, rcon_key: str, rcon_pwd: str, *, timeout: float = 10.0) -> bool:
    try:
        async with rcon_session(host, port, rcon_key, rcon_pwd, timeout=timeout) as client:
            return await client.unban(nucleus_id)
    except Exception as e:
        logger.error(f"在 {host}:{port} 解封玩家失败: {e}")
        return False


async def record_ban(player: Player, reason: str, operator_name: str) -> None:
    await BanRecord.create(player=player, reason=reason, operator=operator_name)
    await Player.filter(id=player.id).update(ban_count=F("ban_count") + 1, status="banned")
    await player_access_service.ensure_uid_blacklist_rule(player, reason, operator_name)


async def record_kick(player: Player) -> None:
    await Player.filter(id=player.id).update(kick_count=F("kick_count") + 1, status="kicked")


async def record_kick_offline(player: Player) -> None:
    await Player.filter(id=player.id).update(kick_count=F("kick_count") + 1)


async def mark_status_kicked(player: Player) -> None:
    await Player.filter(id=player.id).update(status="kicked")


async def record_unban(nucleus_id: int) -> None:
    await Player.filter(nucleus_id=nucleus_id).update(status="offline")
    server_cache.clear_ban_location(nucleus_id)
    await player_access_service.disable_uid_blacklist_rule(nucleus_id)


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
        logger.error("后台解封已中止: 缺少 RCON 配置")
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
            logger.error(f"在 {server_key} 解封玩家失败: {e}")

    if success_count > 0:
        await Player.filter(id=player_id).update(status="offline")
        server_cache.clear_ban_location(nucleus_id)

    logger.info(f"玩家 {nucleus_id} 的后台解封任务已完成: success={success_count}/{len(servers)}")


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


async def list_bans(
    *,
    page_size: int,
    offset: int,
    is_admin: bool = False,
    player_query: str | None = None,
    player_name: str | None = None,
    nucleus_id: int | None = None,
) -> tuple[list[dict], int]:
    operations = await PlayerAccessOperation.filter(
        action__in=["ban", "kick"],
        target_type__in=["player", "uid"],
    ).order_by("-created_at", "-id").prefetch_related("player")
    bans = await BanRecord.all().order_by("-created_at", "-id").prefetch_related("player")
    kicked_players = await Player.filter(status="kicked").order_by("-updated_at", "-id")
    has_player_query, player_ids, exact_targets = await _exact_player_search(
        player_query,
        player_name=player_name,
        nucleus_id=nucleus_id,
    )
    if has_player_query:
        operations = [
            operation
            for operation in operations
            if _operation_matches_exact_player(operation, player_ids=player_ids, exact_targets=exact_targets)
        ]
        bans = [ban for ban in bans if _ban_matches_exact_player(ban, player_ids=player_ids)]
        kicked_players = [player for player in kicked_players if player.id in player_ids]

    notice_by_operation_id = await _notice_by_operation_id([operation.id for operation in operations])
    operation_rows = [
        await _serialize_access_operation_row(
            operation,
            notice_by_operation_id.get(operation.id),
            is_admin=is_admin,
        )
        for operation in operations
    ]

    rows = operation_rows
    for ban in bans:
        if _ban_has_access_operation(ban, operations):
            continue
        rows.append(await _serialize_legacy_ban_row(ban, is_admin=is_admin))
    for player in kicked_players:
        if _player_has_action_operation(player, "kick", operations):
            continue
        rows.append(await _serialize_legacy_kick_row(player, is_admin=is_admin))

    rows.sort(key=lambda item: (item["operation_created_at"], item["id"]), reverse=True)
    return rows[offset : offset + page_size], len(rows)


async def _exact_player_search(
    player_query: str | None,
    *,
    player_name: str | None = None,
    nucleus_id: int | None = None,
) -> tuple[bool, set[int], set[str]]:
    query = str(player_query or "").strip()
    exact_name = str(player_name or "").strip()
    exact_uid = str(nucleus_id).strip() if nucleus_id is not None else ""

    if not exact_name and not exact_uid and query:
        if query.isdigit():
            exact_uid = query
        else:
            exact_name = query

    exact_targets = {_normalize_exact_target(value) for value in (exact_name, exact_uid) if value}
    if not exact_targets:
        return False, set(), set()

    player_filter: Q | None = None
    if exact_name:
        player_filter = Q(name__iexact=exact_name)
    if exact_uid:
        uid_filter = Q(nucleus_id=int(exact_uid))
        player_filter = uid_filter if player_filter is None else player_filter | uid_filter

    assert player_filter is not None
    players = await Player.filter(player_filter)
    return True, {player.id for player in players}, exact_targets


async def _notice_by_operation_id(operation_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not operation_ids:
        return {}

    rows = await PlayerAccessNotice.filter(operation_id__in=operation_ids).values(
        "operation_id",
        "requires_ack",
        "acknowledged_at",
        "message_context",
    )
    return {int(row["operation_id"]): row for row in rows if row.get("operation_id") is not None}


def _player_payload(player: Player | None, *, is_admin: bool) -> dict[str, Any] | None:
    if player is None:
        return None

    payload: dict[str, Any] = {
        "name": player.name,
        "nucleus_id": player.nucleus_id,
        "kick_count": player.kick_count,
        "ban_count": player.ban_count,
        "status": player.status,
        "country": player.country,
    }
    if is_admin:
        payload.update({
            "region": player.region,
            "ip": player.ip,
        })
    return payload


def _operation_target(operation: PlayerAccessOperation) -> str | None:
    target = str(operation.normalized_target or operation.target_value or "").strip()
    return target or None


def _operation_matches_exact_player(operation: PlayerAccessOperation, *, player_ids: set[int], exact_targets: set[str]) -> bool:
    if getattr(operation, "player_id", None) in player_ids:
        return True
    target = _operation_target(operation)
    return bool(target and _normalize_exact_target(target) in exact_targets)


def _ban_matches_exact_player(ban: BanRecord, *, player_ids: set[int]) -> bool:
    return getattr(ban, "player_id", None) in player_ids


def _operation_ip(operation: PlayerAccessOperation, notice: dict[str, Any] | None, player: Player | None) -> str | None:
    result = operation.result if isinstance(operation.result, dict) else {}
    player_ip = str(result.get("player_ip") or "").strip()
    if player_ip:
        return player_ip

    context = notice.get("message_context") if notice else None
    if isinstance(context, dict):
        player_ip = str(context.get("player_ip") or "").strip()
        if player_ip:
            return player_ip

    if player is not None:
        player_ip = str(player.ip or "").strip()
        if player_ip:
            return player_ip
    return None


async def _self_unban_player_by_exact_search(
    *,
    player_name: str | None,
    nucleus_id: int | None,
) -> tuple[Player | None, dict | None]:
    exact_name = str(player_name or "").strip()
    if not exact_name and nucleus_id is None:
        return None, error(ErrorCode.INVALID_REASON, "请提供玩家名或 Nucleus ID")

    query = Player.all()
    if exact_name:
        query = query.filter(name=exact_name)
    if nucleus_id is not None:
        query = query.filter(nucleus_id=nucleus_id)

    players = await query.limit(2)
    if not players:
        target = nucleus_id if nucleus_id is not None else exact_name
        return None, error(ErrorCode.PLAYER_NOT_FOUND, msg=f"未找到玩家 {target}")
    if len(players) > 1:
        return None, error(ErrorCode.INVALID_REASON, "玩家名存在多个精确匹配，请使用 Nucleus ID")
    if not players[0].nucleus_id:
        return None, error(ErrorCode.PLAYER_NO_NUCLEUS_ID, msg=f"玩家 {players[0].name} 没有 nucleus_id")
    return players[0], None


async def self_unban_player(
    *,
    player_name: str | None = None,
    nucleus_id: int | None = None,
    operation_id: int | None = None,
    confirmation_text: str | None = None,
) -> tuple[dict | None, dict | None]:
    if str(confirmation_text or "").strip() != SELF_UNBAN_CONFIRMATION_TEXT:
        return None, error(ErrorCode.INVALID_REASON, f"请输入“{SELF_UNBAN_CONFIRMATION_TEXT}”确认已了解规则")

    player, err = await _self_unban_player_by_exact_search(player_name=player_name, nucleus_id=nucleus_id)
    if err or not player:
        return None, err

    notice_query = PlayerAccessNotice.filter(
        player_id=player.id,
        uid=str(player.nucleus_id),
        action="kick",
        requires_ack=True,
        acknowledged_at__isnull=True,
    )
    if operation_id is not None:
        notice_query = notice_query.filter(operation_id=operation_id)

    notice = await notice_query.order_by("-created_at", "-id").first()
    if not notice:
        return None, error(ErrorCode.INVALID_REASON, "没有可自助解封的待确认记录")

    linked_rule_ids = [f"kick_notice:{notice.id}"]
    operation = await player_access_service.create_access_operation(
        action="ack",
        target_type="uid",
        target_value=player.nucleus_id,
        normalized_target=player.nucleus_id,
        server_scope=notice.server_scope,
        server_id=notice.server_id,
        reason=notice.reason,
        operator="self",
        player=player,
        result={"notice_id": notice.id, "self_unban": True},
        linked_rule_ids=linked_rule_ids,
    )
    updated_notice = await player_access_service.acknowledge_access_notice(notice)

    if player.status == "kicked":
        await Player.filter(id=player.id).update(status="offline")
        player.status = "offline"  # type: ignore[assignment]

    result = {
        "player": _player_payload(player, is_admin=False),
        "notice": player_access_service.serialize_access_notice(updated_notice),
        "operation": player_access_service.serialize_access_operation(operation),
        "self_unban": True,
    }
    operation = await player_access_service.update_access_operation_result(
        operation,
        result={"notice_id": notice.id, "self_unban": True},
        linked_rule_ids=linked_rule_ids,
    )
    result["operation"] = player_access_service.serialize_access_operation(operation)
    return result, None


async def _access_state(player: Player | None, *, target_type: str, target_value: str | None) -> dict[str, Any]:
    if player is not None:
        return await player_access_service.get_player_access_state(player=player)
    if target_type == "uid" and target_value:
        return await player_access_service.get_player_access_state(uid=target_value)
    return await player_access_service.get_player_access_state(uid=None)


async def _serialize_access_operation_row(
    operation: PlayerAccessOperation,
    notice: dict[str, Any] | None,
    *,
    is_admin: bool,
) -> dict[str, Any]:
    player = getattr(operation, "player", None)
    target_value = _operation_target(operation)
    operation_ip = _operation_ip(operation, notice, player)
    action = str(operation.action or "").strip().lower()

    return {
        "id": operation.id,
        "source": "access_operation",
        "action": action,
        "operation_id": operation.id,
        "ban_record_id": None,
        "target_type": operation.target_type,
        "target_value": target_value,
        "server_scope": operation.server_scope,
        "server_id": operation.server_id,
        "reason": operation.reason,
        "operator": operation.operator,
        "remark": operation.remark,
        "operation_ip": operation_ip if is_admin else None,
        "operation_created_at": operation.created_at,
        "created_at": operation.created_at,
        "acknowledged_at": notice.get("acknowledged_at") if notice else None,
        "requires_ack": bool(notice.get("requires_ack")) if notice else False,
        "can_self_unban": action == "kick" and bool(notice and notice.get("requires_ack")),
        "linked_rule_ids": operation.linked_rule_ids,
        "player": _player_payload(player, is_admin=is_admin),
        "access": await _access_state(player, target_type=operation.target_type, target_value=target_value),
    }


async def _serialize_legacy_ban_row(ban: BanRecord, *, is_admin: bool) -> dict[str, Any]:
    operation_ip = str(ban.player.ip or "").strip() or None
    return {
        "id": ban.id,
        "source": "ban_record",
        "action": "ban",
        "operation_id": None,
        "ban_record_id": ban.id,
        "target_type": "player",
        "target_value": str(ban.player.nucleus_id) if ban.player.nucleus_id is not None else None,
        "server_scope": "global",
        "server_id": None,
        "reason": ban.reason,
        "operator": ban.operator,
        "remark": None,
        "operation_ip": operation_ip if is_admin else None,
        "operation_created_at": ban.created_at,
        "created_at": ban.created_at,
        "acknowledged_at": None,
        "requires_ack": False,
        "can_self_unban": False,
        "linked_rule_ids": None,
        "player": _player_payload(ban.player, is_admin=is_admin),
        "access": await player_access_service.get_player_access_state(player=ban.player),
    }


async def _serialize_legacy_kick_row(player: Player, *, is_admin: bool) -> dict[str, Any]:
    operation_ip = str(player.ip or "").strip() or None
    return {
        "id": player.id,
        "source": "player_status",
        "action": "kick",
        "operation_id": None,
        "ban_record_id": None,
        "target_type": "player",
        "target_value": str(player.nucleus_id) if player.nucleus_id is not None else None,
        "server_scope": "global",
        "server_id": None,
        "reason": None,
        "operator": None,
        "remark": None,
        "operation_ip": operation_ip if is_admin else None,
        "operation_created_at": player.updated_at,
        "created_at": player.updated_at,
        "acknowledged_at": None,
        "requires_ack": False,
        "can_self_unban": False,
        "linked_rule_ids": None,
        "player": _player_payload(player, is_admin=is_admin),
        "access": await player_access_service.get_player_access_state(player=player),
    }


def _ban_has_access_operation(ban: BanRecord, operations: list[PlayerAccessOperation]) -> bool:
    ban_player_id = getattr(ban, "player_id", None)
    for operation in operations:
        if operation.action != "ban":
            continue
        if getattr(operation, "player_id", None) != ban_player_id:
            continue
        if (operation.reason or "") != (ban.reason or ""):
            continue
        if (operation.operator or "") != (ban.operator or ""):
            continue
        if _is_close_datetime(operation.created_at, ban.created_at):
            return True
    return False


def _player_has_action_operation(player: Player, action: str, operations: list[PlayerAccessOperation]) -> bool:
    for operation in operations:
        if operation.action == action and getattr(operation, "player_id", None) == player.id:
            return True
    return False


def _is_close_datetime(left: Any, right: Any, *, seconds: int = 10) -> bool:
    try:
        delta = left - right
    except TypeError:
        delta = left.replace(tzinfo=None) - right.replace(tzinfo=None)
    return abs(delta.total_seconds()) <= seconds
