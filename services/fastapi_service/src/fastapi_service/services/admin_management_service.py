from __future__ import annotations

import asyncio
import ipaddress
from datetime import datetime, timedelta
from typing import Any

from shared_lib.models import Player, PlayerAccessNotice, PlayerAccessOperation, PlayerAccessRule, Server, UserBinding
from tortoise.expressions import F, Q

from fastapi_service.core.cache import server_cache
from fastapi_service.core.constants import ALLOWED_REASONS
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error
from fastapi_service.core.utils import CN_TZ
from fastapi_service.services import admin_service, player_access_service, player_service

_DISPLAY_STATUS_SCAN_BATCH_SIZE = 500
_NON_ONLINE_DB_STATUSES = ("banned", "kicked")


def _server_key(host: str | None, port: int | None) -> str | None:
    if not host or not port:
        return None
    return f"{host}:{port}"


def _normalize_scope(scope: str | None) -> str:
    normalized = (scope or "global").strip().lower()
    if normalized not in {"global", "server"}:
        raise ValueError("server_scope 必须是 global 或 server")
    return normalized


async def _rule_server_id(
    *,
    server_scope: str,
    server_db_id: int | None = None,
    server_id: str | None = None,
    server_key: str | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
) -> int | None:
    if server_scope == "global":
        return None

    if server_db_id:
        server = await Server.get_or_none(id=server_db_id)
        if not server:
            raise ValueError(f"未找到服务器: {server_db_id}")
        return server.id

    resolved_key = str(server_key or "").strip() or str(server_id or "").strip()
    host = (server_host or "").strip()
    port = server_port
    if not host and resolved_key and ":" in resolved_key:
        host_part, _, port_part = resolved_key.rpartition(":")
        if host_part and port_part.isdigit():
            host = host_part
            port = int(port_part)

    query = Server.all()
    if host and port:
        server = await Server.get_or_none(host=host, port=port)
        if server:
            return server.id
    if host:
        matches = await Server.filter(host=host).limit(2).all()
        if len(matches) == 1:
            return matches[0].id
    if resolved_key:
        server = await query.filter(Q(server_id=resolved_key) | Q(netkey=resolved_key)).first()
        if server:
            return server.id
    address_key = _server_key(host, port)
    if address_key:
        raise ValueError(f"未找到服务器: {address_key}")
    raise ValueError("server_scope=server 时必须提供 server_db_id")


def _access_denied_action(access: dict[str, Any] | None) -> str | None:
    if not access or access.get("allow", True):
        return None
    if access.get("source") == "kick_notice":
        return "kick"

    rule = access.get("rule") or {}
    source_action = str(rule.get("source_action") or "").strip().lower()
    if source_action in {"ban", "kick"}:
        return source_action
    return "ban"


def _display_status(player: Player, online_location: dict | None, access: dict[str, Any] | None) -> str:
    access_action = _access_denied_action(access)
    if player.status == "banned":
        return "ban"
    if access_action == "ban":
        return "ban"
    if player.status == "kicked":
        return "kick"
    if access_action == "kick":
        return "kick"
    if online_location:
        return "online"
    return "offline"


def _normalize_display_status(status: str | None) -> str | None:
    if not status:
        return None
    normalized = status.strip().lower()
    aliases = {
        "banned": "ban",
        "kicked": "kick",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"online", "offline", "ban", "kick"}:
        raise ValueError("status 必须是 online、offline、ban、kick 之一")
    return normalized


def _expires_at(duration_seconds: int | None) -> datetime | None:
    if not duration_seconds:
        return None
    return datetime.now(CN_TZ) + timedelta(seconds=duration_seconds)


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _operation_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "server_scope",
        "server_id",
        "server_db_id",
        "remark",
        "execution_mode",
        "sync_player_ip",
        "ip_synced",
        "ip_sync_reason",
        "expires_at",
        "linked_ip_released_count",
        "operation_reused",
    ):
        if key in result:
            payload[key] = _json_safe_value(result[key])

    if result.get("synced_ip_rule"):
        payload["synced_ip_rule_id"] = result["synced_ip_rule"].get("rule_id")

    snapshot = result.get("operation_snapshot") or {}
    if isinstance(snapshot, dict):
        player_ip = snapshot.get("player_ip") or snapshot.get("ip")
        player_country = snapshot.get("player_country") or snapshot.get("country")
        player_region = snapshot.get("player_region") or snapshot.get("region")
        if player_ip:
            payload["player_ip"] = player_ip
        if player_country:
            payload["player_country"] = player_country
        if player_region:
            payload["player_region"] = player_region

    notice = result.get("notice") or {}
    notice_context = notice.get("message_context") if isinstance(notice, dict) else None
    if isinstance(notice_context, dict):
        if notice_context.get("player_ip") and not payload.get("player_ip"):
            payload["player_ip"] = notice_context["player_ip"]
        if notice_context.get("player_country") and not payload.get("player_country"):
            payload["player_country"] = notice_context["player_country"]
        if notice_context.get("player_region") and not payload.get("player_region"):
            payload["player_region"] = notice_context["player_region"]

    player = result.get("player") or {}
    if isinstance(player, dict):
        if player.get("ip") and not payload.get("player_ip"):
            payload["player_ip"] = player["ip"]
        if player.get("country") and not payload.get("player_country"):
            payload["player_country"] = player["country"]
        if player.get("region") and not payload.get("player_region"):
            payload["player_region"] = player["region"]
    if result.get("notice"):
        payload["notice_id"] = result["notice"].get("id")
    if result.get("released_rules") is not None:
        payload["released_rule_ids"] = [rule.get("rule_id") for rule in result["released_rules"] if rule.get("rule_id")]
    return payload


def _player_current_ip(player: Player, online_location: dict | None = None) -> str | None:
    if online_location:
        online_ip = str(online_location.get("player_ip") or "").strip()
        if online_ip:
            return online_ip
    return str(player.ip or "").strip() or None


async def _player_operation_snapshot(player: Player, online_location: dict | None = None) -> dict[str, Any]:
    ip = _player_current_ip(player, online_location)
    country = None
    region = None
    if online_location:
        country = online_location.get("player_country")
        region = online_location.get("player_region")

    normalized_ip = player_access_service._normalize_ip(ip) if ip else ""
    player_ip = player_access_service._normalize_ip(player.ip)
    if not (country or region) and normalized_ip and player_ip and normalized_ip == player_ip:
        country = player.country
        region = player.region

    if not (country or region) and normalized_ip:
        country, region = await player_access_service._resolve_geo(normalized_ip)

    return {
        "player_ip": normalized_ip or ip,
        "player_country": country,
        "player_region": region,
    }


async def _update_access_operation_metadata(
    operation: PlayerAccessOperation,
    *,
    target_value: object,
    normalized_target: object | None,
    server_scope: str,
    server_id: int | None,
    reason: str | None,
    remark: str | None,
    operator_name: str | None,
) -> PlayerAccessOperation:
    patch = {
        "target_value": str(target_value),
        "normalized_target": str(normalized_target).strip() if normalized_target is not None else None,
        "server_scope": server_scope,
        "server_id": server_id,
        "reason": (reason or "").strip() or None,
        "remark": (remark or "").strip() or None,
        "operator": (operator_name or "").strip() or None,
    }
    await PlayerAccessOperation.filter(id=operation.id).update(**patch)
    for key, value in patch.items():
        setattr(operation, key, value)
    return operation


async def _active_uid_action_rule(
    *,
    player: Player,
    server_scope: str,
    server_id: int | None,
    source_action: str,
) -> PlayerAccessRule | None:
    if not player.nucleus_id:
        return None

    return await (
        PlayerAccessRule
        .filter(
            rule_type="uid",
            action="deny",
            value=str(player.nucleus_id),
            server_scope=server_scope,
            server_id=server_id,
            source_action=source_action,
            enabled=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=datetime.now(CN_TZ)))
        .order_by("-updated_at", "-id")
        .first()
    )


async def _operation_from_rule(rule: PlayerAccessRule | None, action: str) -> PlayerAccessOperation | None:
    operation_id = getattr(rule, "source_operation_id", None) if rule else None
    if not operation_id:
        return None

    operation = await PlayerAccessOperation.get_or_none(id=operation_id)
    if operation and operation.action == action:
        return operation
    return None


async def _record_global_ban_state(
    player: Player,
    *,
    overwrite_existing: bool,
) -> None:
    if not overwrite_existing:
        await Player.filter(id=player.id).update(ban_count=F("ban_count") + 1, status="banned")
        player.ban_count = int(player.ban_count or 0) + 1  # type: ignore[assignment]
        player.status = "banned"  # type: ignore[assignment]
        return

    await Player.filter(id=player.id).update(status="banned")
    player.status = "banned"  # type: ignore[assignment]


async def _ack_pending_kick_notices_for_ban(
    *,
    player: Player,
    server_scope: str,
    server_id: int | None,
) -> list[int]:
    uid = player_access_service.normalize_uid(player.nucleus_id)
    if not uid:
        return []

    notices = await (
        PlayerAccessNotice
        .filter(
            uid=uid,
            action="kick",
            server_scope=server_scope,
            server_id=server_id,
            requires_ack=True,
            acknowledged_at__isnull=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=datetime.now(CN_TZ)))
        .order_by("-created_at", "-id")
    )
    acknowledged_ids: list[int] = []
    for notice in notices:
        await player_access_service.acknowledge_access_notice(notice)
        acknowledged_ids.append(notice.id)
    return acknowledged_ids


async def _create_synced_ip_rule(
    *,
    player: Player,
    operation: PlayerAccessOperation,
    action: str,
    reason: str,
    operator_name: str,
    server_scope: str,
    server_id: int | None,
    remark: str | None,
    online_location: dict | None = None,
    expires_at: Any = None,
) -> tuple[dict[str, Any] | None, str | None]:
    ip = _player_current_ip(player, online_location)
    if not ip:
        return None, "player_ip_missing"

    rule_id = f"{action}:linked_ip:{operation.id}"
    try:
        rule = await PlayerAccessRule.get_or_none(rule_id=rule_id)
        if rule:
            rule = await player_access_service.update_access_rule(
                rule,
                rule_type="ip",
                action="deny",
                value=ip,
                server_scope=server_scope,
                server_id=server_id,
                reason=reason,
                remark=remark,
                rule_id=rule_id,
                operator=operator_name,
                source_action=action,
                expires_at=expires_at,
                enabled=True,
                priority=25,
            )
            await PlayerAccessRule.filter(id=rule.id).update(source_operation_id=operation.id, player_id=player.id)
            rule.source_operation_id = operation.id  # type: ignore[attr-defined]
            rule.player_id = player.id  # type: ignore[attr-defined]
        else:
            rule = await player_access_service.create_access_rule(
                rule_type="ip",
                action="deny",
                value=ip,
                server_scope=server_scope,
                server_id=server_id,
                reason=reason,
                remark=remark,
                rule_id=rule_id,
                operator=operator_name,
                source_action=action,
                source_operation=operation,
                expires_at=expires_at,
                priority=25,
                player=player,
            )
    except ValueError as exc:
        return None, str(exc)

    return player_access_service.serialize_access_rule(rule), None


async def _player_or_error(identifier: int | str) -> tuple[Player | None, dict | None]:
    return await player_service.get_player_by_identifier(identifier)


async def _uid_rules(player: Player) -> list[dict[str, Any]]:
    if not player.nucleus_id:
        return []
    rules = await PlayerAccessRule.filter(rule_type="uid", value=str(player.nucleus_id)).order_by(
        "server_scope",
        "priority",
        "-updated_at",
    )
    return [player_access_service.serialize_access_rule(rule) for rule in rules]


async def _player_qq(player: Player) -> str | None:
    binding = await UserBinding.filter(player_id=player.id, platform="qq").order_by("id").first()
    return binding.platform_uid if binding else None


async def _players_qq_map(players: list[Player]) -> dict[int, str]:
    player_ids = [player.id for player in players]
    if not player_ids:
        return {}

    rows = await UserBinding.filter(player_id__in=player_ids, platform="qq").order_by("id").values("player_id", "platform_uid")
    qq_by_player_id: dict[int, str] = {}
    for row in rows:
        player_id = row.get("player_id")
        platform_uid = str(row.get("platform_uid") or "").strip()
        if player_id is not None and platform_uid and int(player_id) not in qq_by_player_id:
            qq_by_player_id[int(player_id)] = platform_uid
    return qq_by_player_id


async def _pending_kick_notice_for_player(
    *,
    player: Player,
    server_scope: str,
    server_id: int | None,
) -> PlayerAccessNotice | None:
    if not player.nucleus_id:
        return None

    uid = player_access_service.normalize_uid(player.nucleus_id)
    if not uid:
        return None

    return await (
        PlayerAccessNotice
        .filter(
            uid=uid,
            action="kick",
            server_scope=server_scope,
            server_id=server_id,
            requires_ack=True,
            acknowledged_at__isnull=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=datetime.now(CN_TZ)))
        .order_by("-created_at", "-id")
        .first()
    )


async def _confirmed_kick_notice_for_player(
    *,
    player: Player,
    server_scope: str,
    server_id: int | None,
) -> PlayerAccessNotice | None:
    if not player.nucleus_id:
        return None

    uid = player_access_service.normalize_uid(player.nucleus_id)
    if not uid:
        return None

    return await (
        PlayerAccessNotice
        .filter(
            uid=uid,
            action="kick",
            server_scope=server_scope,
            server_id=server_id,
            acknowledged_at__isnull=False,
        )
        .order_by("-created_at", "-id")
        .first()
    )


async def _reuse_pending_kick_notice(
    *,
    player: Player,
    notice: PlayerAccessNotice,
    identifier: int | str,
    reason: str,
    operator_name: str,
    server_scope: str,
    server_id: int | None,
    remark: str | None,
) -> tuple[dict | None, dict | None]:
    previous_reason = str(notice.reason or "").strip()
    next_reason = str(reason or "").strip()
    if previous_reason == next_reason:
        return None, error(
            ErrorCode.INVALID_REASON,
            "已有未确认 kick 记录，请等待玩家确认后再操作",
            data={"notice": player_access_service.serialize_access_notice(notice)},
        )

    notice_context = notice.message_context if isinstance(notice.message_context, dict) else {}
    next_context = dict(notice_context)
    next_context.update(
        {
            "pending_notice_reused": True,
            "reason_updated": True,
            "previous_reason": previous_reason or None,
            "remark": remark,
        }
    )
    await PlayerAccessNotice.filter(id=notice.id).update(
        reason=next_reason or None,
        message_context=next_context,
        updated_at=datetime.now(CN_TZ),
    )
    notice.reason = next_reason or None  # type: ignore[assignment]
    notice.message_context = next_context  # type: ignore[assignment]

    operation = None
    operation_id = getattr(notice, "operation_id", None)
    if operation_id:
        operation = await PlayerAccessOperation.get_or_none(id=operation_id)
    if operation:
        operation = await _update_access_operation_metadata(
            operation,
            target_value=identifier,
            normalized_target=player.nucleus_id,
            server_scope=server_scope,
            server_id=server_id,
            reason=next_reason,
            remark=remark,
            operator_name=operator_name,
        )
        operation_result = operation.result if isinstance(operation.result, dict) else {}
        operation = await player_access_service.update_access_operation_result(
            operation,
            result=operation_result
            | {
                "pending_notice_reused": True,
                "reason_updated": True,
                "previous_reason": previous_reason or None,
                "notice_id": notice.id,
            },
            linked_rule_ids=operation.linked_rule_ids or [f"kick_notice:{notice.id}"],
        )
        rules = await PlayerAccessRule.filter(source_operation_id=operation.id, source_action="kick", enabled=True).all()
        for rule in rules:
            await player_access_service.update_access_rule(
                rule,
                reason=next_reason,
                remark=remark,
                operator=operator_name,
            )

    return {
        "player": await serialize_player_detail(player, access_server_id=server_id, include_history=False),
        "server_scope": server_scope,
        "server_id": server_id,
        "server_db_id": server_id,
        "operation": player_access_service.serialize_access_operation(operation) if operation else None,
        "remark": remark,
        "execution_mode": "sdk_access",
        "notice": player_access_service.serialize_access_notice(notice),
        "requested_action": "kick",
        "action_escalated": False,
        "pending_notice_reused": True,
        "reason_updated": True,
        "previous_reason": previous_reason or None,
    }, None


async def _ban_for_second_kick(
    *,
    player: Player,
    notice: PlayerAccessNotice,
    identifier: int | str,
    reason: str,
    operator_name: str,
    server_scope: str,
    server_id: int | None,
    remark: str | None,
    duration_seconds: int | None,
) -> tuple[dict | None, dict | None]:
    result, err = await ban_player(
        identifier=identifier,
        reason=reason,
        operator_name=operator_name,
        server_scope=server_scope,
        server_db_id=server_id,
        sync_player_ip=False,
        remark=remark,
        duration_seconds=duration_seconds,
        skip_player_ip_sync=True,
    )
    if result:
        escalation_payload = {
            "requested_action": "kick",
            "action_escalated": True,
            "escalated_from_action": "kick",
            "escalated_to_action": "ban",
            "escalation_reason": "acknowledged_kick",
            "previous_notice_id": notice.id,
        }
        result.update(escalation_payload)
        result["previous_notice"] = player_access_service.serialize_access_notice(notice)
        result["player"] = result.get("player") or await serialize_player_detail(player, access_server_id=server_id, include_history=False)
        operation_id = (result.get("operation") or {}).get("id")
        operation = await PlayerAccessOperation.get_or_none(id=operation_id) if operation_id else None
        if operation:
            operation_result = operation.result if isinstance(operation.result, dict) else {}
            operation = await player_access_service.update_access_operation_result(
                operation,
                result=operation_result | escalation_payload | {"superseded_notice_ids": result.get("superseded_notice_ids") or []},
            )
            result["operation"] = player_access_service.serialize_access_operation(operation)
    return result, err


async def _qq_player_ids(query: str) -> list[int]:
    rows = await UserBinding.filter(platform="qq", platform_uid__icontains=query).values("player_id")
    return [int(row["player_id"]) for row in rows if row.get("player_id") is not None]


async def serialize_player_detail(
    player: Player,
    *,
    access_server_id: int | None = None,
    include_history: bool = True,
) -> dict[str, Any]:
    online_location, _ = player_service.get_online_location(player)
    cached_ban_location = player_service.get_cached_ban_location(player.nucleus_id) if player.nucleus_id else None
    access = await player_access_service.get_player_access_state(
        player=player,
        server_id=access_server_id,
    )
    display_status = _display_status(player, online_location, access)
    payload: dict[str, Any] = {
        "id": player.id,
        "name": player.name,
        "nucleus_id": player.nucleus_id,
        "nucleus_hash": player.nucleus_hash,
        "ip": player.ip,
        "country": player.country,
        "region": player.region,
        "ping": player.ping,
        "loss": player.loss,
        "status": player.status,
        "kick_count": player.kick_count,
        "ban_count": player.ban_count,
        "hardware_name": player.hardware_name,
        "input_device": player.input_device,
        "qq": await _player_qq(player),
        "is_admin": player.is_admin,
        "total_playtime_seconds": player.total_playtime_seconds,
        "online_at": player.online_at,
        "created_at": player.created_at,
        "updated_at": player.updated_at,
        "display_status": display_status,
        "online_location": online_location,
        "cached_ban_location": cached_ban_location,
        "access": access,
    }
    if include_history:
        payload["access_rules"] = await _uid_rules(player)
        payload["access_trace"] = await player_access_service.trace_player_access(
            uid=player.nucleus_id,
            ip=player.ip,
            server_id=access_server_id,
            player=player,
            country=player.country,
            region=player.region,
        )
    return payload


def _notice_scope_sort_key(notice: PlayerAccessNotice, server_id: int | None) -> tuple[int, int, int]:
    normalized_server_keys = player_access_service._normalize_server_keys(server_id)
    key_rank = {key: index for index, key in enumerate(normalized_server_keys)}
    matched_rank = key_rank.get(notice.server_id, len(key_rank))
    scope_rank = 0 if notice.server_scope == "server" and matched_rank < len(key_rank) else 1
    return scope_rank, matched_rank, -notice.id


def _best_scoped_rule(rules: list[PlayerAccessRule], server_id: int | None) -> PlayerAccessRule | None:
    if not rules:
        return None
    return sorted(rules, key=lambda rule: player_access_service._scope_sort_key(rule, server_id))[0]


async def _pending_notice_map(uids: set[str], server_id: int | None) -> dict[str, PlayerAccessNotice]:
    if not uids:
        return {}

    notices = await (
        PlayerAccessNotice
        .filter(
            player_access_service._scope_filter(server_id),
            uid__in=list(uids),
            action="kick",
            requires_ack=True,
            acknowledged_at__isnull=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=datetime.now(CN_TZ)))
        .order_by("-created_at", "-id")
    )
    notice_by_uid: dict[str, PlayerAccessNotice] = {}
    for notice in notices:
        existing = notice_by_uid.get(notice.uid)
        if existing is None or _notice_scope_sort_key(notice, server_id) < _notice_scope_sort_key(existing, server_id):
            notice_by_uid[notice.uid] = notice
    return notice_by_uid


async def _exact_rule_map(rule_type: str, action: str, values: set[str], server_id: int | None) -> dict[str, PlayerAccessRule]:
    if not values:
        return {}

    rules = await PlayerAccessRule.filter(
        player_access_service._scope_filter(server_id),
        player_access_service._active_rule_filter(),
        rule_type=rule_type,
        action=action,
        value__in=list(values),
    ).order_by("priority", "id")
    rules_by_value: dict[str, list[PlayerAccessRule]] = {}
    for rule in rules:
        rules_by_value.setdefault(rule.value, []).append(rule)
    return {value: rule for value, rules_for_value in rules_by_value.items() if (rule := _best_scoped_rule(rules_for_value, server_id))}


async def _active_rules(rule_type: str | list[str], action: str, server_id: int | None) -> list[PlayerAccessRule]:
    query = PlayerAccessRule.filter(
        player_access_service._scope_filter(server_id),
        player_access_service._active_rule_filter(),
        action=action,
    )
    if isinstance(rule_type, list):
        query = query.filter(rule_type__in=rule_type)
    else:
        query = query.filter(rule_type=rule_type)
    return await query.order_by("priority", "id")


def _matching_cidr_rule(rules: list[PlayerAccessRule], ip: str, server_id: int | None) -> PlayerAccessRule | None:
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None

    matches: list[PlayerAccessRule] = []
    for rule in rules:
        try:
            if address in ipaddress.ip_network(rule.value, strict=False):
                matches.append(rule)
        except ValueError:
            continue
    return _best_scoped_rule(matches, server_id)


def _matching_geo_rule(rules: list[PlayerAccessRule], player: Player, server_id: int | None) -> PlayerAccessRule | None:
    country_value = str(player.country or "").strip().lower()
    region_value = str(player.region or "").strip().lower()
    if not country_value and not region_value:
        return None

    matches: list[PlayerAccessRule] = []
    for rule in rules:
        rule_value = str(rule.value or "").strip().lower()
        if rule.rule_type == "country" and country_value and rule_value == country_value:
            matches.append(rule)
        elif rule.rule_type == "region" and region_value and rule_value == region_value:
            matches.append(rule)
        elif rule.rule_type == "geo" and rule_value in {country_value, region_value}:
            matches.append(rule)
    return _best_scoped_rule(matches, server_id)


async def _rule_access_decision(rule: PlayerAccessRule, fallback_locale: str) -> dict[str, Any]:
    if rule.action == "allow":
        return player_access_service._rule_decision(rule, locale=fallback_locale)
    locale = await player_access_service._rule_locale(rule, fallback=fallback_locale)
    return player_access_service._rule_decision(rule, locale=locale)


async def _players_access_state_map(players: list[Player], server_id: int | None) -> dict[int, dict[str, Any]]:
    if not players:
        return {}

    uid_by_player_id = {
        player.id: uid
        for player in players
        if (uid := player_access_service.normalize_uid(player.nucleus_id))
    }
    ip_by_player_id = {
        player.id: ip
        for player in players
        if (ip := player_access_service._normalize_ip(player.ip))
    }
    uids = set(uid_by_player_id.values())
    ips = set(ip_by_player_id.values())

    pending_notices = await _pending_notice_map(uids, server_id)
    uid_allow_rules = await _exact_rule_map("uid", "allow", uids, server_id)
    uid_deny_rules = await _exact_rule_map("uid", "deny", uids, server_id)
    ip_allow_rules = await _exact_rule_map("ip", "allow", ips, server_id)
    ip_deny_rules = await _exact_rule_map("ip", "deny", ips, server_id)
    cidr_allow_rules = await _active_rules("cidr", "allow", server_id)
    cidr_deny_rules = await _active_rules("cidr", "deny", server_id)
    geo_deny_rules = await _active_rules(["geo", "country", "region"], "deny", server_id)

    access_by_player_id: dict[int, dict[str, Any]] = {}
    fallback_players: list[Player] = []
    for player in players:
        locale = player_access_service.reason_locale_from_geo(player.country, player.region)
        uid = uid_by_player_id.get(player.id)
        ip = ip_by_player_id.get(player.id)

        if uid and (notice := pending_notices.get(uid)):
            notice_locale = await player_access_service._notice_locale(notice, fallback=locale)
            access_by_player_id[player.id] = player_access_service._notice_decision(notice, locale=notice_locale)
            continue

        if uid and (rule := uid_allow_rules.get(uid)):
            access_by_player_id[player.id] = await _rule_access_decision(rule, locale)
            continue

        if uid and (rule := uid_deny_rules.get(uid)):
            access_by_player_id[player.id] = await _rule_access_decision(rule, locale)
            continue

        rule = ip_allow_rules.get(ip or "") or _matching_cidr_rule(cidr_allow_rules, ip or "", server_id)
        if rule:
            access_by_player_id[player.id] = await _rule_access_decision(rule, locale)
            continue

        rule = ip_deny_rules.get(ip or "") or _matching_cidr_rule(cidr_deny_rules, ip or "", server_id)
        if rule:
            access_by_player_id[player.id] = await _rule_access_decision(rule, locale)
            continue

        rule = _matching_geo_rule(geo_deny_rules, player, server_id)
        if rule:
            access_by_player_id[player.id] = await _rule_access_decision(rule, locale)
            continue

        if geo_deny_rules and ip and not (player.country or player.region):
            fallback_players.append(player)
            continue

        access_by_player_id[player.id] = player_access_service._default_allow()

    if fallback_players:
        fallback_access = await asyncio.gather(
            *[
                player_access_service.get_player_access_state(
                    player=player,
                    server_id=server_id,
                )
                for player in fallback_players
            ],
        )
        for player, access in zip(fallback_players, fallback_access, strict=True):
            access_by_player_id[player.id] = access

    return access_by_player_id


def _serialize_player_list_item(player: Player, *, qq: str | None, access: dict[str, Any]) -> dict[str, Any]:
    online_location, _ = player_service.get_online_location(player)
    cached_ban_location = player_service.get_cached_ban_location(player.nucleus_id) if player.nucleus_id else None
    display_status = _display_status(player, online_location, access)
    return {
        "id": player.id,
        "name": player.name,
        "nucleus_id": player.nucleus_id,
        "nucleus_hash": player.nucleus_hash,
        "ip": player.ip,
        "country": player.country,
        "region": player.region,
        "ping": player.ping,
        "loss": player.loss,
        "status": player.status,
        "kick_count": player.kick_count,
        "ban_count": player.ban_count,
        "hardware_name": player.hardware_name,
        "input_device": player.input_device,
        "qq": qq,
        "is_admin": player.is_admin,
        "total_playtime_seconds": player.total_playtime_seconds,
        "online_at": player.online_at,
        "created_at": player.created_at,
        "updated_at": player.updated_at,
        "display_status": display_status,
        "online_location": online_location,
        "cached_ban_location": cached_ban_location,
        "access": access,
    }


async def _serialize_player_list_items(players: list[Player], access_server_id: int | None) -> list[dict[str, Any]]:
    qq_by_player_id, access_by_player_id = await asyncio.gather(
        _players_qq_map(players),
        _players_access_state_map(players, access_server_id),
    )
    return [
        _serialize_player_list_item(
            player,
            qq=qq_by_player_id.get(player.id),
            access=access_by_player_id.get(player.id) or player_access_service._default_allow(),
        )
        for player in players
    ]


async def _serialize_player_list_items_with_access(players: list[Player], access_by_player_id: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    qq_by_player_id = await _players_qq_map(players)
    return [
        _serialize_player_list_item(
            player,
            qq=qq_by_player_id.get(player.id),
            access=access_by_player_id.get(player.id) or player_access_service._default_allow(),
        )
        for player in players
    ]


def _display_status_from_online_ids(player: Player, online_nucleus_ids: set[str], access: dict[str, Any] | None) -> str:
    access_action = _access_denied_action(access)
    if player.status == "banned":
        return "ban"
    if access_action == "ban":
        return "ban"
    if player.status == "kicked":
        return "kick"
    if access_action == "kick":
        return "kick"
    if player.nucleus_id is not None and str(player.nucleus_id) in online_nucleus_ids:
        return "online"
    return "offline"


def _online_nucleus_id_values() -> tuple[set[str], list[int]]:
    online_nucleus_ids = {uid for uid in server_cache.get_online_nucleus_ids() if uid.isdigit()}
    return online_nucleus_ids, [int(uid) for uid in online_nucleus_ids]


def _display_status_candidate_query(query: Any, desired_status: str, online_nucleus_id_values: list[int]) -> tuple[Any, bool]:
    if desired_status == "online":
        if not online_nucleus_id_values:
            return query, True
        return query.exclude(status__in=_NON_ONLINE_DB_STATUSES).filter(nucleus_id__in=online_nucleus_id_values), False
    if desired_status == "offline":
        query = query.exclude(status__in=_NON_ONLINE_DB_STATUSES)
        if online_nucleus_id_values:
            query = query.exclude(nucleus_id__in=online_nucleus_id_values)
    return query, False


async def _list_players_by_display_status(
    query: Any,
    *,
    desired_status: str,
    access_server_id: int | None,
    page_size: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    online_nucleus_ids, online_nucleus_id_values = _online_nucleus_id_values()
    query, empty = _display_status_candidate_query(query, desired_status, online_nucleus_id_values)
    if empty:
        return [], 0

    total = 0
    page_players: list[Player] = []
    page_access_by_player_id: dict[int, dict[str, Any]] = {}
    last_id: int | None = None

    while True:
        batch_query = query.filter(id__lt=last_id) if last_id is not None else query
        players = await batch_query.order_by("-id").limit(_DISPLAY_STATUS_SCAN_BATCH_SIZE)
        if not players:
            break

        last_id = players[-1].id
        access_by_player_id = await _players_access_state_map(players, access_server_id)
        for player in players:
            access = access_by_player_id.get(player.id) or player_access_service._default_allow()
            if _display_status_from_online_ids(player, online_nucleus_ids, access) != desired_status:
                continue

            if total >= offset and len(page_players) < page_size:
                page_players.append(player)
                page_access_by_player_id[player.id] = access
            total += 1

    data = await _serialize_player_list_items_with_access(page_players, page_access_by_player_id)
    return data, total


async def list_players(
    *,
    q: str | None = None,
    status: str | None = None,
    nucleus_id: int | None = None,
    ip: str | None = None,
    country: str | None = None,
    region: str | None = None,
    is_admin: bool | None = None,
    access_server_id: int | None = None,
    page_size: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    desired_status = _normalize_display_status(status)
    query = Player.all()
    if q:
        qq_player_ids = await _qq_player_ids(q)
        filter_q = Q(name__icontains=q) | Q(nucleus_hash__icontains=q) | Q(ip__icontains=q)
        if q.isdigit():
            filter_q |= Q(nucleus_id=int(q))
        if qq_player_ids:
            filter_q |= Q(id__in=qq_player_ids)
        query = query.filter(filter_q)
    if nucleus_id:
        query = query.filter(nucleus_id=nucleus_id)
    if ip:
        query = query.filter(ip__icontains=ip)
    if country:
        query = query.filter(country__icontains=country)
    if region:
        query = query.filter(region__icontains=region)
    if is_admin is not None:
        query = query.filter(is_admin=is_admin)

    if desired_status:
        return await _list_players_by_display_status(
            query,
            desired_status=desired_status,
            access_server_id=access_server_id,
            page_size=page_size,
            offset=offset,
        )

    total = await query.count()
    players = await query.order_by("-id").offset(offset).limit(page_size)
    data = await _serialize_player_list_items(players, access_server_id)
    return data, total


async def set_player_admin(
    *,
    identifier: int | str,
    is_admin: bool,
    operator_name: str,
    remark: str | None = None,
) -> tuple[dict | None, dict | None]:
    player, err = await _player_or_error(identifier)
    if err or not player:
        return None, err

    if is_admin:
        has_qq_binding = await UserBinding.filter(player_id=player.id, platform="qq").exists()
        if not has_qq_binding:
            return None, error(ErrorCode.BINDING_NOT_FOUND, "设置管理员前需要先绑定 QQ")

    await Player.filter(id=player.id).update(is_admin=is_admin)
    player.is_admin = is_admin  # type: ignore[assignment]

    operation = await player_access_service.create_access_operation(
        action="admin_set",
        target_type="player",
        target_value=identifier,
        normalized_target=player.nucleus_id,
        reason="ADMIN",
        remark=remark,
        operator=operator_name,
        player=player,
        result={"is_admin": is_admin},
    )

    return {
        "player": await serialize_player_detail(player, include_history=False),
        "operation": player_access_service.serialize_access_operation(operation),
    }, None


async def ban_player(
    *,
    identifier: int | str,
    reason: str,
    operator_name: str,
    server_scope: str = "global",
    server_db_id: int | None = None,
    server_id: str | None = None,
    server_key: str | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
    sync_player_ip: bool = False,
    remark: str | None = None,
    duration_seconds: int | None = None,
    skip_player_ip_sync: bool = False,
) -> tuple[dict | None, dict | None]:
    if reason not in ALLOWED_REASONS:
        return None, error(ErrorCode.INVALID_REASON, f"无效原因。允许值: {ALLOWED_REASONS}")

    sync_player_ip = False if skip_player_ip_sync else True
    player, err = await _player_or_error(identifier)
    if err or not player:
        return None, err

    scope = _normalize_scope(server_scope)
    access_server_id = await _rule_server_id(
        server_scope=scope,
        server_db_id=server_db_id,
        server_id=server_id,
        server_key=server_key,
        server_host=server_host,
        server_port=server_port,
    )
    online_loc, _ = player_service.get_online_location(player)
    operation_snapshot = await _player_operation_snapshot(player, online_loc)
    expires_at = _expires_at(duration_seconds)
    existing_uid_rule = await _active_uid_action_rule(
        player=player,
        server_scope=scope,
        server_id=access_server_id,
        source_action="ban",
    )
    operation = await _operation_from_rule(existing_uid_rule, "ban")
    operation_reused = operation is not None
    if operation:
        operation = await _update_access_operation_metadata(
            operation,
            target_value=identifier,
            normalized_target=player.nucleus_id,
            server_scope=scope,
            server_id=access_server_id,
            reason=reason,
            remark=remark,
            operator_name=operator_name,
        )
    else:
        operation = await player_access_service.create_access_operation(
            action="ban",
            target_type="player",
            target_value=identifier,
            normalized_target=player.nucleus_id,
            server_scope=scope,
            server_id=access_server_id,
            reason=reason,
            remark=remark,
            operator=operator_name,
            player=player,
        )

    uid_rule = None
    if scope == "global":
        await _record_global_ban_state(
            player,
            overwrite_existing=existing_uid_rule is not None or player.status == "banned",
        )
        uid_rule = await player_access_service.ensure_uid_blacklist_rule(
            player,
            reason,
            operator_name,
            remark=remark,
            source_action="ban",
            source_operation=operation,
            expires_at=expires_at,
        )
    else:
        uid_rule = await player_access_service.ensure_uid_blacklist_rule(
            player,
            reason,
            operator_name,
            server_id=access_server_id,
            remark=remark,
            source_action="ban",
            source_operation=operation,
            expires_at=expires_at,
        )

    superseded_notice_ids = await _ack_pending_kick_notices_for_ban(
        player=player,
        server_scope=scope,
        server_id=access_server_id,
    )

    synced_ip_rule = None
    ip_sync_reason = "skip_player_ip_sync" if skip_player_ip_sync else None
    if sync_player_ip:
        synced_ip_rule, ip_sync_reason = await _create_synced_ip_rule(
            player=player,
            operation=operation,
            action="ban",
            reason=reason,
            operator_name=operator_name,
            server_scope=scope,
            server_id=access_server_id,
            remark=remark,
            online_location=online_loc,
            expires_at=expires_at,
        )

    linked_rule_ids = []
    if uid_rule:
        linked_rule_ids.append(uid_rule.rule_id or f"access_rule:{uid_rule.id}")
    if synced_ip_rule:
        linked_rule_ids.append(synced_ip_rule["rule_id"])

    result = {
        "player": await serialize_player_detail(player, access_server_id=access_server_id, include_history=False),
        "server_scope": scope,
        "server_id": access_server_id,
        "server_db_id": access_server_id,
        "operation": player_access_service.serialize_access_operation(operation),
        "remark": remark,
        "execution_mode": "sdk_access",
        "sync_player_ip": sync_player_ip,
        "ip_synced": synced_ip_rule is not None,
        "ip_sync_reason": ip_sync_reason,
        "synced_ip_rule": synced_ip_rule,
        "expires_at": expires_at,
        "operation_reused": operation_reused,
        "superseded_notice_ids": superseded_notice_ids,
        "operation_snapshot": operation_snapshot,
    }
    operation = await player_access_service.update_access_operation_result(
        operation,
        result=_operation_result_payload(result),
        linked_rule_ids=linked_rule_ids,
    )
    result["operation"] = player_access_service.serialize_access_operation(operation)
    return result, None


async def unban_player(
    *,
    identifier: int | str,
    operator_name: str = "admin",
    server_scope: str = "global",
    server_db_id: int | None = None,
    server_id: str | None = None,
    server_key: str | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
    remark: str | None = None,
) -> tuple[dict | None, dict | None]:
    player, err = await _player_or_error(identifier)
    if err or not player:
        return None, err

    scope = _normalize_scope(server_scope)
    access_server_id = await _rule_server_id(
        server_scope=scope,
        server_db_id=server_db_id,
        server_id=server_id,
        server_key=server_key,
        server_host=server_host,
        server_port=server_port,
    )
    operation = await player_access_service.create_access_operation(
        action="unban",
        target_type="player",
        target_value=identifier,
        normalized_target=player.nucleus_id,
        server_scope=scope,
        server_id=access_server_id,
        remark=remark,
        operator=operator_name,
        player=player,
    )

    released_rules = await player_access_service.release_linked_rules_for_uid(
        player.nucleus_id,
        server_id=access_server_id if scope == "server" else None,
    )

    if scope == "global":
        await admin_service.record_unban(player.nucleus_id)
        player.status = "offline"  # type: ignore[assignment]
    else:
        await player_access_service.disable_uid_blacklist_rule(player.nucleus_id, server_id=access_server_id)

    linked_rule_ids = [rule["rule_id"] for rule in released_rules if rule.get("rule_id")]
    result = {
        "player": await serialize_player_detail(player, access_server_id=access_server_id, include_history=False),
        "server_scope": scope,
        "server_id": access_server_id,
        "server_db_id": access_server_id,
        "operation": player_access_service.serialize_access_operation(operation),
        "remark": remark,
        "execution_mode": "sdk_access",
        "released_rules": released_rules,
        "linked_ip_released_count": len([rule for rule in released_rules if rule.get("rule_type") == "ip"]),
    }
    operation = await player_access_service.update_access_operation_result(
        operation,
        result=_operation_result_payload(result),
        linked_rule_ids=linked_rule_ids,
    )
    result["operation"] = player_access_service.serialize_access_operation(operation)
    return result, None


async def kick_player(
    *,
    identifier: int | str,
    reason: str,
    operator_name: str,
    server_scope: str = "global",
    server_db_id: int | None = None,
    server_id: str | None = None,
    server_key: str | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
    sync_player_ip: bool = False,
    remark: str | None = None,
    duration_seconds: int | None = None,
) -> tuple[dict | None, dict | None]:
    if reason not in ALLOWED_REASONS:
        return None, error(ErrorCode.INVALID_REASON, f"无效原因。允许值: {ALLOWED_REASONS}")

    player, err = await _player_or_error(identifier)
    if err or not player:
        return None, err

    scope = _normalize_scope(server_scope)
    access_server_id = await _rule_server_id(
        server_scope=scope,
        server_db_id=server_db_id,
        server_id=server_id,
        server_key=server_key,
        server_host=server_host,
        server_port=server_port,
    )
    existing_notice = await _pending_kick_notice_for_player(
        player=player,
        server_scope=scope,
        server_id=access_server_id,
    )
    if existing_notice:
        return await _reuse_pending_kick_notice(
            player=player,
            notice=existing_notice,
            identifier=identifier,
            reason=reason,
            operator_name=operator_name,
            server_scope=scope,
            server_id=access_server_id,
            remark=remark,
        )

    confirmed_notice = await _confirmed_kick_notice_for_player(
        player=player,
        server_scope=scope,
        server_id=access_server_id,
    )
    if confirmed_notice:
        return await _ban_for_second_kick(
            player=player,
            notice=confirmed_notice,
            identifier=identifier,
            reason=reason,
            operator_name=operator_name,
            server_scope=scope,
            server_id=access_server_id,
            remark=remark,
            duration_seconds=duration_seconds,
        )

    online_loc, _ = player_service.get_online_location(player)
    operation_snapshot = await _player_operation_snapshot(player, online_loc)
    expires_at = _expires_at(duration_seconds)
    operation = await player_access_service.create_access_operation(
        action="kick",
        target_type="player",
        target_value=identifier,
        normalized_target=player.nucleus_id,
        server_scope=scope,
        server_id=access_server_id,
        reason=reason,
        remark=remark,
        operator=operator_name,
        player=player,
    )

    await admin_service.record_kick_offline(player)

    notice = await player_access_service.create_access_notice(
        player=player,
        uid=player.nucleus_id,
        action="kick",
        reason=reason,
        message=None,
        message_context={
            "remark": remark,
            "server_scope": scope,
            "server_id": access_server_id,
            "server_db_id": access_server_id,
            **operation_snapshot,
        },
        server_scope=scope,
        server_id=access_server_id,
        operation=operation,
        expires_at=expires_at,
    )

    synced_ip_rule = None
    ip_sync_reason = None
    if sync_player_ip:
        synced_ip_rule, ip_sync_reason = await _create_synced_ip_rule(
            player=player,
            operation=operation,
            action="kick",
            reason=reason,
            operator_name=operator_name,
            server_scope=scope,
            server_id=access_server_id,
            remark=remark,
            online_location=online_loc,
            expires_at=expires_at,
        )

    linked_rule_ids = [f"kick_notice:{notice.id}"]
    if synced_ip_rule:
        linked_rule_ids.append(synced_ip_rule["rule_id"])

    result = {
        "player": await serialize_player_detail(player, access_server_id=access_server_id, include_history=False),
        "server_scope": scope,
        "server_id": access_server_id,
        "server_db_id": access_server_id,
        "operation": player_access_service.serialize_access_operation(operation),
        "remark": remark,
        "execution_mode": "sdk_access",
        "sync_player_ip": sync_player_ip,
        "ip_synced": synced_ip_rule is not None,
        "ip_sync_reason": ip_sync_reason,
        "synced_ip_rule": synced_ip_rule,
        "notice": player_access_service.serialize_access_notice(notice),
        "expires_at": expires_at,
        "operation_snapshot": operation_snapshot,
    }
    operation = await player_access_service.update_access_operation_result(
        operation,
        result=_operation_result_payload(result),
        linked_rule_ids=linked_rule_ids,
    )
    result["operation"] = player_access_service.serialize_access_operation(operation)
    return result, None


async def apply_access_action(
    *,
    action: str,
    target_type: str,
    target_value: int | str,
    reason: str,
    operator_name: str,
    server_scope: str = "global",
    server_db_id: int | None = None,
    server_id: str | None = None,
    server_key: str | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
    sync_player_ip: bool = False,
    remark: str | None = None,
    duration_seconds: int | None = None,
) -> tuple[dict | None, dict | None]:
    normalized_action = action.strip().lower()
    if normalized_action not in {"ban", "kick", "unban"}:
        return None, error(ErrorCode.INVALID_REASON, "action 必须是 ban、kick 或 unban")
    if normalized_action != "unban" and reason not in ALLOWED_REASONS:
        return None, error(ErrorCode.INVALID_REASON, f"无效原因。允许值: {ALLOWED_REASONS}")

    normalized_target_type = target_type.strip().lower()
    if normalized_target_type == "player":
        if normalized_action == "unban":
            return await unban_player(
                identifier=target_value,
                operator_name=operator_name,
                server_scope=server_scope,
                server_db_id=server_db_id,
                server_id=server_id,
                server_key=server_key,
                server_host=server_host,
                server_port=server_port,
                remark=remark,
            )
        if normalized_action == "ban":
            return await ban_player(
                identifier=target_value,
                reason=reason,
                operator_name=operator_name,
                server_scope=server_scope,
                server_db_id=server_db_id,
                server_id=server_id,
                server_key=server_key,
                server_host=server_host,
                server_port=server_port,
                sync_player_ip=sync_player_ip,
                remark=remark,
                duration_seconds=duration_seconds,
            )
        return await kick_player(
            identifier=target_value,
            reason=reason,
            operator_name=operator_name,
            server_scope=server_scope,
            server_db_id=server_db_id,
            server_id=server_id,
            server_key=server_key,
            server_host=server_host,
            server_port=server_port,
            sync_player_ip=sync_player_ip,
            remark=remark,
            duration_seconds=duration_seconds,
        )

    if normalized_action == "unban":
        return None, error(ErrorCode.INVALID_REASON, "unban 仅支持 target_type=player")

    rule_type_by_target = {
        "uid": "uid",
        "ip": "ip",
        "cidr": "cidr",
        "country": "country",
        "region": "region",
    }
    rule_type = rule_type_by_target.get(normalized_target_type)
    if not rule_type:
        return None, error(ErrorCode.INVALID_REASON, "target_type 必须是 player、uid、ip、cidr、country 或 region")

    scope = _normalize_scope(server_scope)
    access_server_id = await _rule_server_id(
        server_scope=scope,
        server_db_id=server_db_id,
        server_id=server_id,
        server_key=server_key,
        server_host=server_host,
        server_port=server_port,
    )

    try:
        normalized_rule = player_access_service.normalize_access_rule_payload(
            rule_type=rule_type,
            action="deny",
            value=target_value,
            server_scope=scope,
            server_id=access_server_id,
        )
    except ValueError as exc:
        return None, error(ErrorCode.INVALID_REASON, str(exc))

    player = None
    operation_snapshot: dict[str, Any] = {}
    if rule_type == "uid":
        uid_text = normalized_rule["value"]
        if uid_text.isdigit():
            player = await Player.get_or_none(nucleus_id=int(uid_text))
            if player:
                online_loc, _ = player_service.get_online_location(player)
                operation_snapshot = await _player_operation_snapshot(player, online_loc)

    if normalized_action == "kick" and player:
        existing_notice = await _pending_kick_notice_for_player(
            player=player,
            server_scope=scope,
            server_id=access_server_id,
        )
        if existing_notice:
            return await _reuse_pending_kick_notice(
                player=player,
                notice=existing_notice,
                identifier=player.nucleus_id,
                reason=reason,
                operator_name=operator_name,
                server_scope=scope,
                server_id=access_server_id,
                remark=remark,
            )

        confirmed_notice = await _confirmed_kick_notice_for_player(
            player=player,
            server_scope=scope,
            server_id=access_server_id,
        )
        if confirmed_notice:
            return await _ban_for_second_kick(
                player=player,
                notice=confirmed_notice,
                identifier=player.nucleus_id,
                reason=reason,
                operator_name=operator_name,
                server_scope=scope,
                server_id=access_server_id,
                remark=remark,
                duration_seconds=duration_seconds,
            )

    expires_at = _expires_at(duration_seconds)
    operation = await player_access_service.create_access_operation(
        action=normalized_action,
        target_type=normalized_target_type,
        target_value=target_value,
        normalized_target=normalized_rule["value"],
        server_scope=scope,
        server_id=access_server_id,
        reason=reason,
        remark=remark,
        operator=operator_name,
        player=player,
    )

    try:
        rule = await player_access_service.create_access_rule(
            rule_type=rule_type,
            action="deny",
            value=target_value,
            server_scope=scope,
            server_id=access_server_id,
            reason=reason,
            remark=remark,
            rule_id=f"{normalized_action}:{rule_type}:{operation.id}",
            operator=operator_name,
            source_action=normalized_action,
            source_operation=operation,
            expires_at=expires_at,
            priority=30,
            player=player,
        )
    except ValueError as exc:
        return None, error(ErrorCode.INVALID_REASON, str(exc))

    notice = None
    if normalized_action == "kick" and player and player.nucleus_id:
        notice = await player_access_service.create_access_notice(
            player=player,
            uid=player.nucleus_id,
            action="kick",
            reason=reason,
            message=None,
            message_context={
                "remark": remark,
                "server_scope": scope,
                "server_id": access_server_id,
                "target_type": normalized_target_type,
                "target_value": normalized_rule["value"],
                **operation_snapshot,
            },
            server_scope=scope,
            server_id=access_server_id,
            operation=operation,
            expires_at=expires_at,
        )

    linked_rule_ids = [rule.rule_id or f"access_rule:{rule.id}"]
    if notice:
        linked_rule_ids.append(f"kick_notice:{notice.id}")

    result = {
        "operation": player_access_service.serialize_access_operation(operation),
        "rule": player_access_service.serialize_access_rule(rule),
        "notice": player_access_service.serialize_access_notice(notice) if notice else None,
        "server_scope": scope,
        "server_id": access_server_id,
        "server_db_id": access_server_id,
        "remark": remark,
        "execution_mode": "sdk_access",
        "expires_at": expires_at,
        "target_type": normalized_target_type,
        "target_value": normalized_rule["value"],
        "operation_snapshot": operation_snapshot,
    }
    operation = await player_access_service.update_access_operation_result(
        operation,
        result=_operation_result_payload(result)
        | {
            "target_type": normalized_target_type,
            "target_value": normalized_rule["value"],
            "rule_id": rule.rule_id or f"access_rule:{rule.id}",
            "notice_id": notice.id if notice else None,
        },
        linked_rule_ids=linked_rule_ids,
    )
    result["operation"] = player_access_service.serialize_access_operation(operation)
    return result, None
