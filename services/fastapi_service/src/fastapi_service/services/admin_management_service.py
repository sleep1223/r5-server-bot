from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from shared_lib.models import BanRecord, Player, PlayerAccessOperation, PlayerAccessRule, UserBinding
from tortoise.expressions import Q

from fastapi_service.core.cache import server_cache
from fastapi_service.core.constants import ALLOWED_REASONS, is_no_cover_allowed_server
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error
from fastapi_service.core.utils import CN_TZ
from fastapi_service.services import admin_service, player_access_service, player_service
from fastapi_service.services.rcon import require_rcon_config


def _server_key(host: str | None, port: int | None) -> str | None:
    if not host or not port:
        return None
    return f"{host}:{port}"


def _normalize_scope(scope: str | None) -> str:
    normalized = (scope or "global").strip().lower()
    if normalized not in {"global", "server"}:
        raise ValueError("server_scope 必须是 global 或 server")
    return normalized


def _rule_server_id(
    *,
    server_scope: str,
    server_id: str | None = None,
    server_key: str | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
) -> str | None:
    if server_scope == "global":
        return None
    resolved = (server_id or "").strip() or (server_key or "").strip() or _server_key(server_host, server_port)
    if not resolved:
        raise ValueError("server_scope=server 时必须提供 server_id、server_key 或 server_host/server_port")
    return resolved


def _server_matches(
    server: dict,
    *,
    server_id: str | None = None,
    server_key: str | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
) -> bool:
    if server_key and server.get("server_key") == server_key:
        return True

    if server_host and server_port:
        if server.get("server_host") == server_host and int(server.get("server_port") or 0) == server_port:
            return True

    if server_id:
        candidates = {
            str(server.get("server_key") or ""),
            str(server.get("server_name") or ""),
            _server_key(server.get("server_host"), server.get("server_port")) or "",
        }
        return server_id in candidates

    return False


def _select_rcon_servers(
    *,
    server_scope: str,
    server_id: str | None = None,
    server_key: str | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
) -> list[dict]:
    online_servers = server_cache.get_online_servers()
    if server_scope == "global":
        return online_servers

    return [
        server
        for server in online_servers
        if _server_matches(
            server,
            server_id=server_id,
            server_key=server_key,
            server_host=server_host,
            server_port=server_port,
        )
    ]


def _server_payload(server: dict | None) -> dict | None:
    if not server:
        return None
    return {
        "name": server.get("server_name"),
        "host": server.get("server_host"),
        "port": server.get("server_port"),
        "key": server.get("server_key") or _server_key(server.get("server_host"), server.get("server_port")),
    }


def _access_denied_action(access: dict[str, Any] | None) -> str | None:
    if not access or access.get("allow", True):
        return None
    if access.get("source") == "kick_notice":
        return "kick"
    if access.get("source") == "legacy_ban":
        return "ban"

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


def _action_reason(action: str, reason: str | None) -> str | None:
    if not reason:
        return None
    return player_access_service.action_reason_text(action, reason)


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
        "remark",
        "sync_player_ip",
        "ip_synced",
        "ip_sync_reason",
        "expires_at",
        "rcon_skipped",
        "rcon_failed",
        "broadcast_total",
        "broadcast_success_count",
        "linked_ip_released_count",
    ):
        if key in result:
            payload[key] = _json_safe_value(result[key])

    if result.get("hit_servers") is not None:
        payload["hit_servers"] = result["hit_servers"]
    if result.get("hit_server") is not None:
        payload["hit_server"] = result["hit_server"]
    if result.get("synced_ip_rule"):
        payload["synced_ip_rule_id"] = result["synced_ip_rule"].get("rule_id")
    player = result.get("player") or {}
    if isinstance(player, dict) and player.get("ip"):
        payload["player_ip"] = player["ip"]
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


async def _create_synced_ip_rule(
    *,
    player: Player,
    operation: PlayerAccessOperation,
    action: str,
    reason: str,
    operator_name: str,
    server_scope: str,
    server_id: str | None,
    remark: str | None,
    online_location: dict | None = None,
    expires_at: Any = None,
) -> tuple[dict[str, Any] | None, str | None]:
    ip = _player_current_ip(player, online_location)
    if not ip:
        return None, "player_ip_missing"

    try:
        rule = await player_access_service.create_access_rule(
            rule_type="ip",
            action="deny",
            value=ip,
            server_scope=server_scope,
            server_id=server_id,
            reason=_action_reason(action, reason),
            remark=remark,
            rule_id=f"{action}:linked_ip:{operation.id}",
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


async def _recent_bans(player: Player, limit: int = 10) -> list[dict[str, Any]]:
    records = await BanRecord.filter(player=player).order_by("-created_at").limit(limit)
    return [
        {
            "id": record.id,
            "reason": record.reason,
            "operator": record.operator,
            "created_at": record.created_at,
        }
        for record in records
    ]


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


async def _qq_player_ids(query: str) -> list[int]:
    rows = await UserBinding.filter(platform="qq", platform_uid__icontains=query).values("player_id")
    return [int(row["player_id"]) for row in rows if row.get("player_id") is not None]


async def serialize_player_detail(
    player: Player,
    *,
    access_server_id: str | None = None,
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
        payload["recent_bans"] = await _recent_bans(player)
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


async def list_players(
    *,
    q: str | None = None,
    status: str | None = None,
    nucleus_id: int | None = None,
    ip: str | None = None,
    country: str | None = None,
    region: str | None = None,
    is_admin: bool | None = None,
    access_server_id: str | None = None,
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
        matched: list[dict[str, Any]] = []
        players = await query.order_by("-updated_at")
        for player in players:
            item = await serialize_player_detail(
                player,
                access_server_id=access_server_id,
                include_history=False,
            )
            if item["display_status"] == desired_status:
                matched.append(item)
        return matched[offset : offset + page_size], len(matched)

    total = await query.count()
    players = await query.order_by("-updated_at").offset(offset).limit(page_size)
    data = [
        await serialize_player_detail(
            player,
            access_server_id=access_server_id,
            include_history=False,
        )
        for player in players
    ]
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
    access_server_id = _rule_server_id(
        server_scope=scope,
        server_id=server_id,
        server_key=server_key,
        server_host=server_host,
        server_port=server_port,
    )
    targets = _select_rcon_servers(
        server_scope=scope,
        server_id=access_server_id,
        server_key=server_key,
        server_host=server_host,
        server_port=server_port,
    )
    online_loc, _ = player_service.get_online_location(player)
    expires_at = _expires_at(duration_seconds)
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

    if reason == "NO_COVER":
        targets = [server for server in targets if not is_no_cover_allowed_server(server.get("server_host"), server.get("server_name"))]

    success_count = 0
    hits: list[dict] = []
    rcon_skipped = not targets
    rcon_failed = False
    if targets:
        rcon_key, rcon_pwd = require_rcon_config()
        online_server_key = None
        if scope == "global" and online_loc:
            online_server_key = _server_key(online_loc["server_host"], online_loc["server_port"])
        elif scope == "server" and len(targets) == 1:
            online_server_key = targets[0].get("server_key")

        success_count, hits = await admin_service.broadcast_bann_player(
            player.nucleus_id,
            reason,
            targets,
            rcon_key,
            rcon_pwd,
            online_server_key=online_server_key,
        )
        rcon_failed = success_count == 0

    uid_rule = None
    if scope == "global":
        await admin_service.record_ban(player, reason, operator_name)
        uid_rule = await player_access_service.ensure_uid_blacklist_rule(
            player,
            reason,
            operator_name,
            remark=remark,
            source_action="ban",
            source_operation=operation,
            expires_at=expires_at,
        )
        player.status = "banned"  # type: ignore[assignment]
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

    synced_ip_rule = None
    ip_sync_reason = None
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
        "operation": player_access_service.serialize_access_operation(operation),
        "remark": remark,
        "sync_player_ip": sync_player_ip,
        "ip_synced": synced_ip_rule is not None,
        "ip_sync_reason": ip_sync_reason,
        "synced_ip_rule": synced_ip_rule,
        "expires_at": expires_at,
        "rcon_skipped": rcon_skipped,
        "rcon_failed": rcon_failed,
        "broadcast_total": len(targets),
        "broadcast_success_count": success_count,
        "hit_servers": [_server_payload(server) for server in hits],
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
    access_server_id = _rule_server_id(
        server_scope=scope,
        server_id=server_id,
        server_key=server_key,
        server_host=server_host,
        server_port=server_port,
    )
    targets = _select_rcon_servers(
        server_scope=scope,
        server_id=access_server_id,
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

    success_count = 0
    if targets:
        rcon_key, rcon_pwd = require_rcon_config()
        for server in targets:
            ok = await admin_service.unban_player_on_server(
                player.nucleus_id,
                server["server_host"],
                server["server_port"],
                rcon_key,
                rcon_pwd,
                timeout=3.0,
            )
            if ok:
                success_count += 1

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
        "operation": player_access_service.serialize_access_operation(operation),
        "remark": remark,
        "released_rules": released_rules,
        "linked_ip_released_count": len([rule for rule in released_rules if rule.get("rule_type") == "ip"]),
        "rcon_skipped": not targets,
        "broadcast_total": len(targets),
        "broadcast_success_count": success_count,
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
    access_server_id = _rule_server_id(
        server_scope=scope,
        server_id=server_id,
        server_key=server_key,
        server_host=server_host,
        server_port=server_port,
    )
    targets = _select_rcon_servers(
        server_scope=scope,
        server_id=access_server_id,
        server_key=server_key,
        server_host=server_host,
        server_port=server_port,
    )
    online_loc, _ = player_service.get_online_location(player)
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

    if reason == "NO_COVER":
        targets = [server for server in targets if not is_no_cover_allowed_server(server.get("server_host"), server.get("server_name"))]

    await admin_service.record_kick_offline(player)
    success_count = 0
    hit_server = None
    if targets:
        rcon_key, rcon_pwd = require_rcon_config()
        success_count, hit_server = await admin_service.broadcast_kick_player(
            player.nucleus_id,
            reason,
            targets,
            rcon_key,
            rcon_pwd,
        )
        if hit_server:
            await admin_service.mark_status_kicked(player)
            player.status = "kicked"  # type: ignore[assignment]

    notice = await player_access_service.create_access_notice(
        player=player,
        uid=player.nucleus_id,
        action="kick",
        reason=reason,
        message=_action_reason("kick", reason),
        message_context={
            "remark": remark,
            "server_scope": scope,
            "server_id": access_server_id,
            "player_ip": _player_current_ip(player, online_loc),
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
        "operation": player_access_service.serialize_access_operation(operation),
        "remark": remark,
        "sync_player_ip": sync_player_ip,
        "ip_synced": synced_ip_rule is not None,
        "ip_sync_reason": ip_sync_reason,
        "synced_ip_rule": synced_ip_rule,
        "notice": player_access_service.serialize_access_notice(notice),
        "expires_at": expires_at,
        "rcon_skipped": not targets,
        "broadcast_total": len(targets),
        "broadcast_success_count": success_count,
        "hit_server": _server_payload(hit_server),
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
    server_id: str | None = None,
    server_key: str | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
    sync_player_ip: bool = False,
    remark: str | None = None,
    duration_seconds: int | None = None,
) -> tuple[dict | None, dict | None]:
    normalized_action = action.strip().lower()
    if normalized_action not in {"ban", "kick"}:
        return None, error(ErrorCode.INVALID_REASON, "action 必须是 ban 或 kick")
    if reason not in ALLOWED_REASONS:
        return None, error(ErrorCode.INVALID_REASON, f"无效原因。允许值: {ALLOWED_REASONS}")

    normalized_target_type = target_type.strip().lower()
    if normalized_target_type == "player":
        if normalized_action == "ban":
            return await ban_player(
                identifier=target_value,
                reason=reason,
                operator_name=operator_name,
                server_scope=server_scope,
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
            server_id=server_id,
            server_key=server_key,
            server_host=server_host,
            server_port=server_port,
            sync_player_ip=sync_player_ip,
            remark=remark,
            duration_seconds=duration_seconds,
        )

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
    access_server_id = _rule_server_id(
        server_scope=scope,
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
    if rule_type == "uid":
        uid_text = normalized_rule["value"]
        if uid_text.isdigit():
            player = await Player.get_or_none(nucleus_id=int(uid_text))

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
            reason=_action_reason(normalized_action, reason),
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
            message=_action_reason("kick", reason),
            message_context={
                "remark": remark,
                "server_scope": scope,
                "server_id": access_server_id,
                "target_type": normalized_target_type,
                "target_value": normalized_rule["value"],
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
        "remark": remark,
        "expires_at": expires_at,
        "target_type": normalized_target_type,
        "target_value": normalized_rule["value"],
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
