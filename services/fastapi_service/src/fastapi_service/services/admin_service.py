from typing import Any

from shared_lib.models import Player, PlayerAccessNotice, PlayerAccessOperation
from tortoise.expressions import F, Q

from fastapi_service.core.cache import server_cache
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error

from . import player_access_service

SELF_UNBAN_CONFIRMATION_TEXT = "我已了解规则"


def _normalize_exact_target(value: object | None) -> str:
    return str(value or "").strip().casefold()


async def record_ban(player: Player, reason: str, operator_name: str) -> None:
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


async def list_bans(
    *,
    page_size: int,
    offset: int,
    is_admin: bool = False,
    player_query: str | None = None,
    player_name: str | None = None,
    nucleus_id: int | None = None,
    acknowledged: bool | None = None,
) -> tuple[list[dict], int]:
    operations = (
        await PlayerAccessOperation
        .filter(
            action__in=["ban", "kick"],
            target_type__in=["player", "uid"],
        )
        .order_by("-created_at", "-id")
        .prefetch_related("player")
    )
    unban_operations = (
        await PlayerAccessOperation
        .filter(
            action="unban",
            target_type__in=["player", "uid"],
        )
        .order_by("-created_at", "-id")
        .prefetch_related("player")
    )
    has_player_query, player_ids, exact_targets = await _exact_player_search(
        player_query,
        player_name=player_name,
        nucleus_id=nucleus_id,
    )
    if has_player_query:
        operations = [operation for operation in operations if _operation_matches_exact_player(operation, player_ids=player_ids, exact_targets=exact_targets)]
        unban_operations = [operation for operation in unban_operations if _operation_matches_exact_player(operation, player_ids=player_ids, exact_targets=exact_targets)]

    notice_by_operation_id = await _notice_by_operation_id([operation.id for operation in operations])
    rows = [
        await _serialize_access_operation_row(
            operation,
            notice_by_operation_id.get(operation.id),
            is_admin=is_admin,
        )
        for operation in operations
    ]

    _apply_unban_resolution(rows, unban_operations)
    _suppress_duplicate_pending_kick_rows(rows)
    rows = _filter_rows_by_acknowledged(rows, acknowledged)
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
        "action",
        "requires_ack",
        "acknowledged_at",
        "message_context",
    )
    return {int(row["operation_id"]): row for row in rows if row.get("operation_id") is not None}


def _player_payload(
    player: Player | None,
    *,
    is_admin: bool,
    country: str | None = None,
    region: str | None = None,
    ip: str | None = None,
) -> dict[str, Any] | None:
    if player is None:
        return None

    payload: dict[str, Any] = {
        "id": player.id,
        "name": player.name,
        "nucleus_id": player.nucleus_id,
        "kick_count": player.kick_count,
        "ban_count": player.ban_count,
        "status": player.status,
        "country": country if country is not None else player.country,
        "input_device": player.input_device or "unknown",
    }
    if is_admin:
        payload.update({
            "region": region if region is not None else player.region,
            "ip": ip if ip is not None else player.ip,
        })
    return payload


def _resolution_payload(status: str, resolved_at: Any = None) -> dict[str, Any]:
    return {
        "resolution_status": status,
        "resolved_at": resolved_at,
    }


def _filter_rows_by_acknowledged(rows: list[dict[str, Any]], acknowledged: bool | None) -> list[dict[str, Any]]:
    if acknowledged is None:
        return rows
    if acknowledged:
        return [row for row in rows if row.get("acknowledged_at")]
    return [row for row in rows if row.get("requires_ack") and not row.get("acknowledged_at")]


def _row_player_id(row: dict[str, Any]) -> int | None:
    player = row.get("player")
    if not isinstance(player, dict):
        return None
    try:
        return int(player["id"])
    except (KeyError, TypeError, ValueError):
        return None


def _kick_row_dedupe_key(row: dict[str, Any]) -> tuple[object, str, str | None] | None:
    if row.get("action") != "kick":
        return None

    player_id = _row_player_id(row)
    player_key: object = player_id if player_id is not None else _normalize_exact_target(row.get("target_value"))
    if not player_key:
        return None
    return player_key, str(row.get("server_scope") or "global"), row.get("server_id")


def _suppress_duplicate_pending_kick_rows(rows: list[dict[str, Any]]) -> None:
    pending_by_key: dict[tuple[object, str, str | None], dict[str, Any]] = {}
    for row in rows:
        if row.get("resolution_status") != "pending":
            continue
        key = _kick_row_dedupe_key(row)
        if key is None:
            continue
        current = pending_by_key.get(key)
        if current is None or (
            row.get("operation_created_at"),
            row.get("operation_id") or row.get("id") or 0,
        ) > (
            current.get("operation_created_at"),
            current.get("operation_id") or current.get("id") or 0,
        ):
            pending_by_key[key] = row

    if not pending_by_key:
        return

    filtered: list[dict[str, Any]] = []
    for row in rows:
        key = _kick_row_dedupe_key(row)
        if key is None or key not in pending_by_key or row is pending_by_key.get(key) or row.get("resolution_status") == "resolved":
            filtered.append(row)
    rows[:] = filtered


def _datetime_gte(left: Any, right: Any) -> bool:
    try:
        return left >= right
    except TypeError:
        return left.replace(tzinfo=None) >= right.replace(tzinfo=None)


def _apply_unban_resolution(rows: list[dict[str, Any]], unban_operations: list[PlayerAccessOperation]) -> None:
    latest_unban_by_player_id: dict[int, PlayerAccessOperation] = {}
    for operation in unban_operations:
        player_id = getattr(operation, "player_id", None)
        if player_id is None:
            continue
        latest_unban_by_player_id.setdefault(int(player_id), operation)

    for row in rows:
        if row.get("action") != "ban":
            continue
        player_id = _row_player_id(row)
        if player_id is None:
            continue
        unban_operation = latest_unban_by_player_id.get(player_id)
        if not unban_operation:
            continue
        if _datetime_gte(unban_operation.created_at, row.get("operation_created_at")):
            row.update(_resolution_payload("resolved", unban_operation.created_at))


def _operation_target(operation: PlayerAccessOperation) -> str | None:
    target = str(operation.normalized_target or operation.target_value or "").strip()
    return target or None


def _operation_matches_exact_player(operation: PlayerAccessOperation, *, player_ids: set[int], exact_targets: set[str]) -> bool:
    if getattr(operation, "player_id", None) in player_ids:
        return True
    target = _operation_target(operation)
    return bool(target and _normalize_exact_target(target) in exact_targets)


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


def _snapshot_context_value(context: dict[str, Any] | None, *keys: str) -> str | None:
    if not isinstance(context, dict):
        return None
    for key in keys:
        value = str(context.get(key) or "").strip()
        if value:
            return value
    return None


async def _operation_snapshot(operation: PlayerAccessOperation, notice: dict[str, Any] | None, player: Player | None) -> dict[str, str | None]:
    result = operation.result if isinstance(operation.result, dict) else {}
    notice_context = notice.get("message_context") if notice else None
    if not isinstance(notice_context, dict):
        notice_context = {}

    operation_ip = player_access_service._normalize_ip(_operation_ip(operation, notice, player)) or _operation_ip(operation, notice, player)
    country = _snapshot_context_value(result, "player_country", "country") or _snapshot_context_value(notice_context, "player_country", "country")
    region = _snapshot_context_value(result, "player_region", "region") or _snapshot_context_value(notice_context, "player_region", "region")

    player_ip = player_access_service._normalize_ip(player.ip) if player else ""
    if not (country or region) and operation_ip and player_ip and operation_ip == player_ip:
        country = player.country if player else None
        region = player.region if player else None

    if not (country or region) and operation_ip:
        country, region = await player_access_service._resolve_geo(operation_ip)

    return {
        "ip": operation_ip,
        "country": country,
        "region": region,
    }


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


async def _serialize_access_operation_row(
    operation: PlayerAccessOperation,
    notice: dict[str, Any] | None,
    *,
    is_admin: bool,
) -> dict[str, Any]:
    player = getattr(operation, "player", None)
    target_value = _operation_target(operation)
    snapshot = await _operation_snapshot(operation, notice, player)
    operation_ip = snapshot["ip"]
    operation_country = snapshot["country"]
    operation_region = snapshot["region"]
    action = str(operation.action or "").strip().lower()
    acknowledged_at = notice.get("acknowledged_at") if notice else None
    requires_ack = bool(notice.get("requires_ack")) if notice else False
    notice_action = str(notice.get("action") or "").strip().lower() if notice else ""
    if action == "kick" and notice_action == "kick" and acknowledged_at:
        resolution = _resolution_payload("resolved", acknowledged_at)
    elif action == "kick" and notice_action == "kick" and requires_ack:
        resolution = _resolution_payload("pending")
    elif action == "kick":
        resolution = _resolution_payload("active")
    else:
        resolution = _resolution_payload("active")

    return {
        "id": operation.id,
        "source": "access_operation",
        "action": action,
        "operation_id": operation.id,
        "target_type": operation.target_type,
        "target_value": target_value,
        "server_scope": operation.server_scope,
        "server_id": operation.server_id,
        "reason": operation.reason,
        "operator": operation.operator,
        "remark": operation.remark,
        "operation_ip": operation_ip if is_admin else None,
        "operation_country": operation_country,
        "operation_region": operation_region,
        "country": operation_country,
        "region": operation_region,
        "operation_created_at": operation.created_at,
        "created_at": operation.created_at,
        "acknowledged_at": acknowledged_at,
        "requires_ack": requires_ack,
        "can_self_unban": (action == "kick" and bool(notice) and notice_action == "kick" and requires_ack and not acknowledged_at),
        **resolution,
        "linked_rule_ids": operation.linked_rule_ids,
        "player": _player_payload(player, is_admin=is_admin, country=operation_country, region=operation_region, ip=operation_ip if is_admin else None),
    }
