from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Any

from loguru import logger
from shared_lib.models import BanRecord, Player, PlayerAccessNotice, PlayerAccessOperation, PlayerAccessRule
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from fastapi_service.core.cache import server_cache
from fastapi_service.core.utils import CN_TZ, generate_hash, resolve_ips_batch

RULE_TYPES = {"uid", "ip", "cidr", "geo", "country", "region"}
RULE_ACTIONS = {"allow", "deny"}
SERVER_SCOPES = {"global", "server"}


def normalize_uid(uid: object | None, nucleus_id: object | None = None) -> str:
    for value in (uid, nucleus_id):
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _uid_to_int(uid: str) -> int | None:
    if not uid.isdigit():
        return None
    try:
        return int(uid)
    except ValueError:
        return None


def _normalize_ip(ip: object | None) -> str:
    text = str(ip or "").strip()
    if not text:
        return ""
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text


def _input_device_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("inputDevice", "input_device", "input", "device"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text[:50]
    return None


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _ban_rule_id(uid: str) -> str:
    return f"legacy_ban:{uid}"


def _scoped_ban_rule_id(uid: str, server_id: str | None) -> str:
    if server_id:
        return f"server_ban:{server_id}:{uid}"
    return _ban_rule_id(uid)


def _ban_reason(reason: str | None) -> str:
    return f"#BAN_REASON_{reason}" if reason else "禁止进入该服务器"


def _kick_reason(reason: str | None) -> str:
    return f"#KICK_REASON_{reason}" if reason else "请访问网站确认后再进入服务器"


def _normalize_server_scope(server_scope: object | None, server_id: object | None = None) -> tuple[str, str | None]:
    scope = str(server_scope or "global").strip().lower()
    if scope not in SERVER_SCOPES:
        raise ValueError("server_scope 必须是 global 或 server")

    normalized_server_id = str(server_id or "").strip() or None
    if scope == "server" and not normalized_server_id:
        raise ValueError("server_scope=server 时必须提供 server_id")
    if scope == "global":
        normalized_server_id = None
    return scope, normalized_server_id


def _scope_filter(server_id: object | None = None) -> Q:
    normalized_server_id = str(server_id or "").strip()
    if normalized_server_id:
        return Q(server_scope="global") | Q(server_scope="server", server_id=normalized_server_id)
    return Q(server_scope="global")


def _active_rule_filter() -> Q:
    now = _now()
    return Q(enabled=True) & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))


def _scope_sort_key(rule: PlayerAccessRule, server_id: object | None = None) -> tuple[int, int, int]:
    normalized_server_id = str(server_id or "").strip()
    scope_rank = 0 if normalized_server_id and rule.server_scope == "server" and rule.server_id == normalized_server_id else 1
    return scope_rank, rule.priority, rule.id


def _default_allow() -> dict[str, Any]:
    return {
        "allow": True,
        "reason": None,
        "rule_id": None,
        "rule_type": None,
        "server_scope": None,
        "server_id": None,
        "source": "default_allow",
    }


def _rule_decision(rule: PlayerAccessRule) -> dict[str, Any]:
    allow = rule.action == "allow"
    return {
        "allow": allow,
        "reason": None if allow else rule.reason,
        "rule_id": rule.rule_id or f"access_rule:{rule.id}",
        "rule_type": rule.rule_type,
        "server_scope": rule.server_scope,
        "server_id": rule.server_id,
        "rule": serialize_access_rule(rule),
        "source": "player_access_rule",
    }


def _legacy_ban_decision(uid: str, reason: str | None, operator: str | None) -> dict[str, Any]:
    return {
        "allow": False,
        "reason": _ban_reason(reason),
        "rule_id": _ban_rule_id(uid),
        "rule_type": "uid",
        "server_scope": "global",
        "server_id": None,
        "source": "legacy_ban",
        "operator": operator,
    }


def _notice_decision(notice: PlayerAccessNotice) -> dict[str, Any]:
    return {
        "allow": False,
        "reason": notice.message or _kick_reason(notice.reason),
        "rule_id": f"kick_notice:{notice.id}",
        "rule_type": "notice",
        "server_scope": notice.server_scope,
        "server_id": notice.server_id,
        "source": "kick_notice",
        "notice": serialize_access_notice(notice),
    }


def action_from_access_decision(decision: dict[str, Any]) -> str | None:
    if decision.get("allow", True):
        return None
    if decision.get("source") == "kick_notice":
        return "kick"
    if decision.get("source") == "legacy_ban":
        return "ban"

    rule = decision.get("rule") or {}
    source_action = str(rule.get("source_action") or "").strip().lower()
    if source_action in {"ban", "kick"}:
        return source_action

    return "kick"


async def _find_player_for_uid(uid: str) -> Player | None:
    if not uid:
        return None

    filter_q = Q(nucleus_hash=generate_hash(uid))
    uid_int = _uid_to_int(uid)
    if uid_int is not None:
        filter_q |= Q(nucleus_id=uid_int)

    try:
        return await Player.filter(filter_q).first()
    except Exception as exc:
        logger.warning(f"准入 uid={uid} 的玩家查询失败: {exc}")
        return None


async def _first_exact_rule(
    rule_type: str,
    action: str,
    value: str,
    *,
    server_id: object | None = None,
) -> PlayerAccessRule | None:
    try:
        rules = await PlayerAccessRule.filter(
            _scope_filter(server_id),
            _active_rule_filter(),
            rule_type=rule_type,
            action=action,
            value=value,
        ).order_by("priority", "id")
        if not rules:
            return None
        return sorted(rules, key=lambda r: _scope_sort_key(r, server_id))[0]
    except Exception as exc:
        logger.warning(f"玩家准入精确规则查询失败: {exc}")
        return None


async def _matching_cidr_rule(action: str, ip: str, *, server_id: object | None = None) -> PlayerAccessRule | None:
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None

    try:
        rules = await PlayerAccessRule.filter(
            _scope_filter(server_id),
            _active_rule_filter(),
            rule_type="cidr",
            action=action,
        ).order_by("priority", "id")
    except Exception as exc:
        logger.warning(f"玩家准入 CIDR 规则查询失败: {exc}")
        return None

    matches = []
    for rule in rules:
        try:
            if address in ipaddress.ip_network(rule.value, strict=False):
                matches.append(rule)
        except ValueError:
            logger.warning(f"玩家准入 CIDR 规则值无效: value={rule.value!r}")
    if not matches:
        return None
    return sorted(matches, key=lambda r: _scope_sort_key(r, server_id))[0]


async def _resolve_geo(ip: str) -> tuple[str | None, str | None]:
    if not ip:
        return None, None
    try:
        resolved = await resolve_ips_batch([ip])
    except Exception as exc:
        logger.warning(f"玩家准入 IP 解析失败: ip={ip}, error={exc}")
        return None, None

    info = resolved.get(ip) or {}
    country = info.get("country")
    region = info.get("region")
    return country, region


async def _matching_geo_deny_rule(
    *,
    ip: str,
    country: str | None,
    region: str | None,
    server_id: object | None = None,
) -> PlayerAccessRule | None:
    try:
        rules = await PlayerAccessRule.filter(
            _scope_filter(server_id),
            _active_rule_filter(),
            rule_type__in=["geo", "country", "region"],
            action="deny",
        ).order_by("priority", "id")
    except Exception as exc:
        logger.warning(f"玩家准入地理规则查询失败: {exc}")
        return None

    if not rules:
        return None

    if not country and not region and ip:
        country, region = await _resolve_geo(ip)

    country_value = str(country or "").strip().lower()
    region_value = str(region or "").strip().lower()
    if not country_value and not region_value:
        return None

    matches = []
    for rule in rules:
        rule_value = str(rule.value or "").strip().lower()
        if rule.rule_type == "country" and country_value and rule_value == country_value:
            matches.append(rule)
        elif rule.rule_type == "region" and region_value and rule_value == region_value:
            matches.append(rule)
        elif rule.rule_type == "geo" and rule_value in {country_value, region_value}:
            matches.append(rule)
    if not matches:
        return None
    return sorted(matches, key=lambda r: _scope_sort_key(r, server_id))[0]


async def _legacy_ban_for_uid(uid: str, player: Player | None) -> dict[str, Any] | None:
    player = player or await _find_player_for_uid(uid)
    if not player or player.status != "banned":
        return None

    record = await BanRecord.filter(player=player).order_by("-created_at").first()
    if record is None:
        return _legacy_ban_decision(uid, "banned", None)
    return _legacy_ban_decision(uid, record.reason, record.operator)


async def _pending_notice_for_uid(uid: str, server_id: object | None = None) -> PlayerAccessNotice | None:
    if not uid:
        return None

    try:
        notices = await PlayerAccessNotice.filter(
            _scope_filter(server_id),
            uid=uid,
            requires_ack=True,
            acknowledged_at__isnull=True,
        ).order_by("-created_at", "-id")
    except Exception as exc:
        logger.warning(f"玩家准入通知查询失败: uid={uid}, error={exc}")
        return None

    now = _now()
    active_notices = [notice for notice in notices if notice.expires_at is None or notice.expires_at > now]
    if not active_notices:
        return None

    normalized_server_id = str(server_id or "").strip()

    def _notice_sort_key(notice: PlayerAccessNotice) -> tuple[int, int]:
        scope_rank = 0 if normalized_server_id and notice.server_scope == "server" and notice.server_id == normalized_server_id else 1
        return scope_rank, -notice.id

    return sorted(active_notices, key=_notice_sort_key)[0]


async def evaluate_player_access(
    *,
    uid: object | None,
    ip: object | None = None,
    server_id: object | None = None,
    player: Player | None = None,
    country: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """Evaluate access using the SDK document's fixed priority order."""
    uid_text = normalize_uid(uid)
    ip_text = _normalize_ip(ip)

    if uid_text:
        notice = await _pending_notice_for_uid(uid_text, server_id=server_id)
        if notice:
            return _notice_decision(notice)

        rule = await _first_exact_rule("uid", "allow", uid_text, server_id=server_id)
        if rule:
            return _rule_decision(rule)

        rule = await _first_exact_rule("uid", "deny", uid_text, server_id=server_id)
        if rule:
            return _rule_decision(rule)

        legacy_ban = await _legacy_ban_for_uid(uid_text, player)
        if legacy_ban:
            return legacy_ban

    if ip_text:
        rule = await _first_exact_rule("ip", "allow", ip_text, server_id=server_id) or await _matching_cidr_rule("allow", ip_text, server_id=server_id)
        if rule:
            return _rule_decision(rule)

        rule = await _first_exact_rule("ip", "deny", ip_text, server_id=server_id) or await _matching_cidr_rule("deny", ip_text, server_id=server_id)
        if rule:
            return _rule_decision(rule)

    rule = await _matching_geo_deny_rule(ip=ip_text, country=country, region=region, server_id=server_id)
    if rule:
        return _rule_decision(rule)

    return _default_allow()


def _trace_record(step: str, matched: bool, rule: PlayerAccessRule | PlayerAccessNotice | None = None, note: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"step": step, "matched": matched}
    if isinstance(rule, PlayerAccessRule):
        item["rule"] = serialize_access_rule(rule)
    elif isinstance(rule, PlayerAccessNotice):
        item["notice"] = serialize_access_notice(rule)
    if note:
        item["note"] = note
    return item


async def trace_player_access(
    *,
    uid: object | None,
    ip: object | None = None,
    server_id: object | None = None,
    player: Player | None = None,
    country: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    uid_text = normalize_uid(uid)
    ip_text = _normalize_ip(ip)
    checks: list[dict[str, Any]] = []

    if uid_text:
        notice = await _pending_notice_for_uid(uid_text, server_id=server_id)
        checks.append(_trace_record("kick_notice", notice is not None, notice))
        if notice:
            decision = _notice_decision(notice)
            return {"decision": decision, "checks": checks, "matched_rules": [decision]}

        rule = await _first_exact_rule("uid", "allow", uid_text, server_id=server_id)
        checks.append(_trace_record("uid_allow", rule is not None, rule))
        if rule:
            decision = _rule_decision(rule)
            return {"decision": decision, "checks": checks, "matched_rules": [decision]}

        rule = await _first_exact_rule("uid", "deny", uid_text, server_id=server_id)
        checks.append(_trace_record("uid_deny", rule is not None, rule))
        if rule:
            decision = _rule_decision(rule)
            return {"decision": decision, "checks": checks, "matched_rules": [decision]}

        legacy_ban = await _legacy_ban_for_uid(uid_text, player)
        checks.append({
            "step": "legacy_ban",
            "matched": legacy_ban is not None,
            "decision": legacy_ban,
        })
        if legacy_ban:
            return {"decision": legacy_ban, "checks": checks, "matched_rules": [legacy_ban]}
    else:
        checks.append(_trace_record("uid", False, note="uid_missing"))

    if ip_text:
        rule = await _first_exact_rule("ip", "allow", ip_text, server_id=server_id) or await _matching_cidr_rule("allow", ip_text, server_id=server_id)
        checks.append(_trace_record("ip_cidr_allow", rule is not None, rule))
        if rule:
            decision = _rule_decision(rule)
            return {"decision": decision, "checks": checks, "matched_rules": [decision]}

        rule = await _first_exact_rule("ip", "deny", ip_text, server_id=server_id) or await _matching_cidr_rule("deny", ip_text, server_id=server_id)
        checks.append(_trace_record("ip_cidr_deny", rule is not None, rule))
        if rule:
            decision = _rule_decision(rule)
            return {"decision": decision, "checks": checks, "matched_rules": [decision]}
    else:
        checks.append(_trace_record("ip", False, note="ip_missing"))

    rule = await _matching_geo_deny_rule(ip=ip_text, country=country, region=region, server_id=server_id)
    checks.append(_trace_record("geo_deny", rule is not None, rule))
    if rule:
        decision = _rule_decision(rule)
        return {"decision": decision, "checks": checks, "matched_rules": [decision]}

    decision = _default_allow()
    checks.append({"step": "default_allow", "matched": True, "decision": decision})
    return {"decision": decision, "checks": checks, "matched_rules": []}


async def upsert_access_player_snapshot(
    *,
    uid: object | None,
    nucleus_id: object | None = None,
    player_name: str | None = None,
    ip: object | None = None,
    input_device: str | None = None,
) -> Player | None:
    """Store identity facts from the SDK access callback without changing online status."""
    uid_text = normalize_uid(uid, nucleus_id)
    if not uid_text:
        return None

    nucleus_hash = generate_hash(uid_text)
    nucleus_int = _uid_to_int(uid_text)
    player = await _find_player_for_uid(uid_text)
    ip_text = _normalize_ip(ip)
    country, region = await _resolve_geo(ip_text) if ip_text else (None, None)

    if player:
        updates: dict[str, Any] = {"nucleus_hash": nucleus_hash}
        if nucleus_int is not None:
            updates["nucleus_id"] = nucleus_int
        if player_name:
            updates["name"] = player_name
        if ip_text:
            updates["ip"] = ip_text
        if country:
            updates["country"] = country
        if region:
            updates["region"] = region
        if input_device:
            updates["input_device"] = input_device

        try:
            await Player.filter(id=player.id).update(**updates)
        except Exception as exc:
            logger.warning(f"玩家准入快照更新失败: uid={uid_text}, error={exc}")

        for key, value in updates.items():
            setattr(player, key, value)
        return player

    defaults: dict[str, Any] = {
        "nucleus_hash": nucleus_hash,
        "name": player_name or uid_text,
        "ip": ip_text or None,
        "country": country,
        "region": region,
        "input_device": input_device,
    }
    if nucleus_int is not None:
        defaults["nucleus_id"] = nucleus_int

    try:
        return await Player.create(**defaults)
    except IntegrityError:
        return await _find_player_for_uid(uid_text)


async def check_player_access(
    *,
    uid: object | None,
    nucleus_id: object | None,
    player_name: str | None,
    ip: object | None,
    port: int | None = None,
    server_id: str | None = None,
) -> dict[str, Any]:
    player = await upsert_access_player_snapshot(
        uid=uid,
        nucleus_id=nucleus_id,
        player_name=player_name,
        ip=ip,
    )
    return await evaluate_player_access(
        uid=normalize_uid(uid, nucleus_id),
        ip=ip,
        server_id=server_id,
        player=player,
        country=player.country if player else None,
        region=player.region if player else None,
    )


async def process_online_players_report(
    *,
    server_id: str,
    report: dict[str, Any],
) -> dict[str, Any]:
    server_cache.update_access_report(server_id, report)

    actions: list[dict[str, Any]] = []
    for player_payload in report.get("players") or []:
        uid_text = normalize_uid(player_payload.get("uid"), player_payload.get("nucleusId"))
        if not uid_text:
            continue

        player = await upsert_access_player_snapshot(
            uid=uid_text,
            nucleus_id=player_payload.get("nucleusId"),
            player_name=player_payload.get("playerName"),
            ip=player_payload.get("ip"),
            input_device=_input_device_from_payload(player_payload),
        )
        decision = await evaluate_player_access(
            uid=uid_text,
            ip=player_payload.get("ip"),
            server_id=server_id,
            player=player,
            country=player.country if player else player_payload.get("country"),
            region=player.region if player else player_payload.get("region"),
        )
        action = action_from_access_decision(decision)
        if not action:
            continue

        action_payload: dict[str, Any] = {
            "uid": uid_text,
            "action": action,
            "reason": decision.get("reason"),
            "ruleId": decision.get("rule_id"),
        }
        nucleus_id = _uid_to_int(uid_text)
        if nucleus_id is not None:
            action_payload["nucleusId"] = nucleus_id
        actions.append(action_payload)

    return {"actions": actions}


async def ensure_uid_blacklist_rule(
    player: Player,
    reason: str | None,
    operator_name: str | None,
    *,
    server_id: object | None = None,
    remark: str | None = None,
    source_action: str | None = "ban",
    source_operation: PlayerAccessOperation | None = None,
    expires_at: datetime | None = None,
) -> PlayerAccessRule | None:
    if not player.nucleus_id:
        return None

    uid = str(player.nucleus_id)
    scope, normalized_server_id = _normalize_server_scope("server" if server_id else "global", server_id)
    rule_id = _scoped_ban_rule_id(uid, normalized_server_id)
    try:
        rule = await PlayerAccessRule.get_or_none(rule_id=rule_id)
        values = {
            "rule_type": "uid",
            "action": "deny",
            "value": uid,
            "server_scope": scope,
            "server_id": normalized_server_id,
            "reason": _ban_reason(reason),
            "remark": (remark or "").strip() or None,
            "operator": operator_name,
            "source_action": source_action,
            "source_operation_id": source_operation.id if source_operation else None,
            "expires_at": expires_at,
            "enabled": True,
            "priority": 20,
            "player_id": player.id,
            "updated_at": _now(),
        }
        if rule:
            await PlayerAccessRule.filter(id=rule.id).update(**values)
            for key, value in values.items():
                setattr(rule, key, value)
            return rule

        return await PlayerAccessRule.create(
            rule_id=rule_id,
            created_at=_now(),
            **values,
        )
    except Exception as exc:
        logger.warning(f"同步玩家准入封禁规则失败: uid={uid}, error={exc}")
        return None


async def disable_uid_blacklist_rule(nucleus_id: int, *, server_id: object | None = None) -> None:
    uid = str(nucleus_id)
    rule_id = _scoped_ban_rule_id(uid, str(server_id).strip() if server_id else None)
    try:
        await PlayerAccessRule.filter(
            rule_id=rule_id,
            enabled=True,
        ).update(enabled=False, updated_at=_now())
    except Exception as exc:
        logger.warning(f"禁用玩家准入封禁规则失败: uid={uid}, error={exc}")


def _normalize_rule_value(rule_type: str, value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("value 不能为空")

    if rule_type == "uid":
        normalized = normalize_uid(text)
        if not normalized:
            raise ValueError("uid value 不能为空")
        return normalized

    if rule_type == "ip":
        try:
            return str(ipaddress.ip_address(text))
        except ValueError as exc:
            raise ValueError("value 必须是有效 IP 地址") from exc

    if rule_type == "cidr":
        try:
            return str(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise ValueError("value 必须是有效 CIDR") from exc

    return text


def normalize_access_rule_payload(
    *,
    rule_type: object,
    action: object,
    value: object,
    server_scope: object | None = None,
    server_id: object | None = None,
) -> dict[str, Any]:
    normalized_type = str(rule_type or "").strip().lower()
    if normalized_type not in RULE_TYPES:
        raise ValueError(f"rule_type 必须是以下值之一: {sorted(RULE_TYPES)}")

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in RULE_ACTIONS:
        raise ValueError(f"action 必须是以下值之一: {sorted(RULE_ACTIONS)}")

    normalized_scope, normalized_server_id = _normalize_server_scope(server_scope, server_id)
    return {
        "rule_type": normalized_type,
        "action": normalized_action,
        "value": _normalize_rule_value(normalized_type, value),
        "server_scope": normalized_scope,
        "server_id": normalized_server_id,
    }


def serialize_access_rule(rule: PlayerAccessRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "rule_type": rule.rule_type,
        "action": rule.action,
        "value": rule.value,
        "server_scope": rule.server_scope,
        "server_id": rule.server_id,
        "reason": rule.reason,
        "remark": rule.remark,
        "rule_id": rule.rule_id,
        "operator": rule.operator,
        "source_action": rule.source_action,
        "source_operation_id": getattr(rule, "source_operation_id", None),
        "expires_at": rule.expires_at,
        "enabled": rule.enabled,
        "priority": rule.priority,
        "player_id": getattr(rule, "player_id", None),
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


def serialize_access_operation(operation: PlayerAccessOperation) -> dict[str, Any]:
    return {
        "id": operation.id,
        "action": operation.action,
        "target_type": operation.target_type,
        "target_value": operation.target_value,
        "normalized_target": operation.normalized_target,
        "server_scope": operation.server_scope,
        "server_id": operation.server_id,
        "reason": operation.reason,
        "remark": operation.remark,
        "operator": operation.operator,
        "player_id": getattr(operation, "player_id", None),
        "result": operation.result,
        "linked_rule_ids": operation.linked_rule_ids,
        "created_at": operation.created_at,
    }


def serialize_access_notice(notice: PlayerAccessNotice) -> dict[str, Any]:
    return {
        "id": notice.id,
        "player_id": getattr(notice, "player_id", None),
        "uid": notice.uid,
        "action": notice.action,
        "reason": notice.reason,
        "message": notice.message,
        "message_context": notice.message_context,
        "server_scope": notice.server_scope,
        "server_id": notice.server_id,
        "requires_ack": notice.requires_ack,
        "acknowledged_at": notice.acknowledged_at,
        "expires_at": notice.expires_at,
        "operation_id": getattr(notice, "operation_id", None),
        "created_at": notice.created_at,
        "updated_at": notice.updated_at,
    }


async def list_access_rules(
    *,
    q: str | None = None,
    rule_type: str | None = None,
    action: str | None = None,
    server_scope: str | None = None,
    server_id: str | None = None,
    enabled: bool | None = None,
    page_size: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    query = PlayerAccessRule.all()
    if q:
        query = query.filter(Q(value__icontains=q) | Q(reason__icontains=q) | Q(remark__icontains=q) | Q(rule_id__icontains=q))
    if rule_type:
        query = query.filter(rule_type=rule_type)
    if action:
        query = query.filter(action=action)
    if server_scope:
        query = query.filter(server_scope=server_scope)
    if server_id:
        query = query.filter(server_id=server_id)
    if enabled is not None:
        query = query.filter(enabled=enabled)

    total = await query.count()
    rules = await query.order_by("server_scope", "priority", "-updated_at").offset(offset).limit(page_size)
    return [serialize_access_rule(rule) for rule in rules], total


async def get_access_rule(rule_id: int) -> PlayerAccessRule | None:
    return await PlayerAccessRule.get_or_none(id=rule_id)


async def create_access_rule(
    *,
    rule_type: object,
    action: object,
    value: object,
    server_scope: object | None = None,
    server_id: object | None = None,
    reason: str | None = None,
    remark: str | None = None,
    rule_id: str | None = None,
    operator: str | None = None,
    source_action: str | None = None,
    source_operation: PlayerAccessOperation | None = None,
    expires_at: datetime | None = None,
    enabled: bool = True,
    priority: int = 100,
    player: Player | None = None,
) -> PlayerAccessRule:
    payload = normalize_access_rule_payload(
        rule_type=rule_type,
        action=action,
        value=value,
        server_scope=server_scope,
        server_id=server_id,
    )
    return await PlayerAccessRule.create(
        **payload,
        reason=(reason or "").strip() or None,
        remark=(remark or "").strip() or None,
        rule_id=(rule_id or "").strip() or None,
        operator=(operator or "").strip() or None,
        source_action=(source_action or "").strip() or None,
        source_operation_id=source_operation.id if source_operation else None,
        expires_at=expires_at,
        enabled=enabled,
        priority=priority,
        player_id=player.id if player else None,
    )


async def update_access_rule(rule: PlayerAccessRule, **updates: Any) -> PlayerAccessRule:
    normalized: dict[str, Any] = {}
    if {"rule_type", "action", "value", "server_scope", "server_id"} & updates.keys():
        normalized = normalize_access_rule_payload(
            rule_type=updates.get("rule_type", rule.rule_type),
            action=updates.get("action", rule.action),
            value=updates.get("value", rule.value),
            server_scope=updates.get("server_scope", rule.server_scope),
            server_id=updates.get("server_id", rule.server_id),
        )

    patch: dict[str, Any] = {**normalized}
    for key in ("reason", "remark", "rule_id", "operator", "source_action", "enabled", "priority", "expires_at"):
        if key in updates:
            value = updates[key]
            if key in {"reason", "remark", "rule_id", "operator", "source_action"}:
                patch[key] = str(value or "").strip() or None
            else:
                patch[key] = value

    if not patch:
        return rule

    patch["updated_at"] = _now()
    await PlayerAccessRule.filter(id=rule.id).update(**patch)
    for key, value in patch.items():
        setattr(rule, key, value)
    return rule


async def disable_access_rule(rule: PlayerAccessRule) -> PlayerAccessRule:
    await PlayerAccessRule.filter(id=rule.id).update(enabled=False, updated_at=_now())
    rule.enabled = False  # type: ignore[assignment]
    return rule


async def create_access_operation(
    *,
    action: str,
    target_type: str,
    target_value: object,
    normalized_target: object | None = None,
    server_scope: object | None = "global",
    server_id: object | None = None,
    reason: str | None = None,
    remark: str | None = None,
    operator: str | None = None,
    player: Player | None = None,
    result: dict[str, Any] | None = None,
    linked_rule_ids: list[str] | None = None,
) -> PlayerAccessOperation:
    scope, normalized_server_id = _normalize_server_scope(server_scope, server_id)
    return await PlayerAccessOperation.create(
        action=action,
        target_type=target_type,
        target_value=str(target_value),
        normalized_target=str(normalized_target).strip() if normalized_target is not None else None,
        server_scope=scope,
        server_id=normalized_server_id,
        reason=(reason or "").strip() or None,
        remark=(remark or "").strip() or None,
        operator=(operator or "").strip() or None,
        player_id=player.id if player else None,
        result=result,
        linked_rule_ids=linked_rule_ids,
    )


async def update_access_operation_result(
    operation: PlayerAccessOperation,
    *,
    result: dict[str, Any] | None = None,
    linked_rule_ids: list[str] | None = None,
) -> PlayerAccessOperation:
    patch: dict[str, Any] = {}
    if result is not None:
        patch["result"] = result
    if linked_rule_ids is not None:
        patch["linked_rule_ids"] = linked_rule_ids
    if patch:
        await PlayerAccessOperation.filter(id=operation.id).update(**patch)
        for key, value in patch.items():
            setattr(operation, key, value)
    return operation


async def list_access_operations(
    *,
    action: str | None = None,
    target_type: str | None = None,
    q: str | None = None,
    player_id: int | None = None,
    server_scope: str | None = None,
    server_id: str | None = None,
    page_size: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    query = PlayerAccessOperation.all()
    if action:
        query = query.filter(action=action)
    if target_type:
        query = query.filter(target_type=target_type)
    if q:
        query = query.filter(Q(target_value__icontains=q) | Q(normalized_target__icontains=q) | Q(remark__icontains=q))
    if player_id:
        query = query.filter(player_id=player_id)
    if server_scope:
        query = query.filter(server_scope=server_scope)
    if server_id:
        query = query.filter(server_id=server_id)

    total = await query.count()
    operations = await query.order_by("-created_at", "-id").offset(offset).limit(page_size)
    return [serialize_access_operation(operation) for operation in operations], total


async def create_access_notice(
    *,
    player: Player,
    uid: object,
    action: str,
    reason: str | None,
    message: str | None,
    message_context: dict[str, Any] | None,
    server_scope: object | None,
    server_id: object | None,
    operation: PlayerAccessOperation | None = None,
    expires_at: datetime | None = None,
) -> PlayerAccessNotice:
    scope, normalized_server_id = _normalize_server_scope(server_scope, server_id)
    return await PlayerAccessNotice.create(
        player_id=player.id,
        uid=normalize_uid(uid),
        action=action,
        reason=(reason or "").strip() or None,
        message=(message or "").strip() or None,
        message_context=message_context,
        server_scope=scope,
        server_id=normalized_server_id,
        requires_ack=True,
        operation_id=operation.id if operation else None,
        expires_at=expires_at,
    )


async def list_access_notices(
    *,
    uid: str | None = None,
    requires_ack: bool | None = None,
    acknowledged: bool | None = None,
    server_scope: str | None = None,
    server_id: str | None = None,
    page_size: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    query = PlayerAccessNotice.all()
    if uid:
        query = query.filter(uid=uid)
    if requires_ack is not None:
        query = query.filter(requires_ack=requires_ack)
    if acknowledged is True:
        query = query.exclude(acknowledged_at__isnull=True)
    elif acknowledged is False:
        query = query.filter(acknowledged_at__isnull=True)
    if server_scope:
        query = query.filter(server_scope=server_scope)
    if server_id:
        query = query.filter(server_id=server_id)

    total = await query.count()
    notices = await query.order_by("-created_at", "-id").offset(offset).limit(page_size)
    return [serialize_access_notice(notice) for notice in notices], total


async def get_access_notice(notice_id: int) -> PlayerAccessNotice | None:
    return await PlayerAccessNotice.get_or_none(id=notice_id)


async def acknowledge_access_notice(notice: PlayerAccessNotice) -> PlayerAccessNotice:
    now = _now()
    await PlayerAccessNotice.filter(id=notice.id).update(acknowledged_at=now, requires_ack=False, updated_at=now)
    notice.acknowledged_at = now  # type: ignore[assignment]
    notice.requires_ack = False  # type: ignore[assignment]
    operation_id = getattr(notice, "operation_id", None)
    if operation_id:
        await PlayerAccessRule.filter(source_operation_id=operation_id, source_action="kick", enabled=True).update(
            enabled=False,
            updated_at=now,
        )
    return notice


async def release_linked_rules_for_uid(
    uid: object,
    *,
    server_id: object | None = None,
) -> list[dict[str, Any]]:
    uid_text = normalize_uid(uid)
    if not uid_text:
        return []

    query = PlayerAccessRule.filter(rule_type="uid", value=uid_text, enabled=True)
    if server_id:
        query = query.filter(server_scope="server", server_id=str(server_id).strip())
    else:
        query = query.filter(server_scope="global")

    uid_rules = await query
    operation_ids = [operation_id for rule in uid_rules if (operation_id := getattr(rule, "source_operation_id", None))]
    now = _now()

    released: list[PlayerAccessRule] = []
    if uid_rules:
        await PlayerAccessRule.filter(id__in=[rule.id for rule in uid_rules]).update(enabled=False, updated_at=now)
        for rule in uid_rules:
            rule.enabled = False  # type: ignore[assignment]
            rule.updated_at = now  # type: ignore[assignment]
        released.extend(uid_rules)

    if operation_ids:
        linked_rules = await PlayerAccessRule.filter(
            source_operation_id__in=operation_ids,
            rule_type__in=["ip"],
            enabled=True,
        )
        if linked_rules:
            await PlayerAccessRule.filter(id__in=[rule.id for rule in linked_rules]).update(enabled=False, updated_at=now)
            for rule in linked_rules:
                rule.enabled = False  # type: ignore[assignment]
                rule.updated_at = now  # type: ignore[assignment]
            released.extend(linked_rules)

    return [serialize_access_rule(rule) for rule in released]


async def get_player_access_state(
    *,
    player: Player | None = None,
    uid: object | None = None,
    ip: object | None = None,
    server_id: object | None = None,
) -> dict[str, Any]:
    uid_value = normalize_uid(uid if uid is not None else (player.nucleus_id if player else None))
    return await evaluate_player_access(
        uid=uid_value,
        ip=ip if ip is not None else (player.ip if player else None),
        server_id=server_id,
        player=player,
        country=player.country if player else None,
        region=player.region if player else None,
    )


async def build_online_player_info(player_data: dict, *, is_admin: bool = False, server_id: object | None = None) -> dict[str, Any]:
    uid = normalize_uid(player_data.get("uniqueid"))
    player = await _find_player_for_uid(uid) if uid else None
    access = await evaluate_player_access(
        uid=uid,
        ip=player_data.get("ip"),
        server_id=server_id,
        player=player,
        country=player_data.get("country"),
        region=player_data.get("region"),
    )

    info: dict[str, Any] = {
        "name": player_data.get("name", "Unknown"),
        "country": player_data.get("country"),
    }
    if is_admin:
        uid_int = _uid_to_int(uid)
        info.update({
            "region": player_data.get("region"),
            "nucleus_id": uid_int if uid_int is not None else (uid or None),
            "ip": player_data.get("ip"),
            "input_device": _input_device_from_payload(player_data) or (player.input_device if player else None),
            "ping": player_data.get("ping", 0),
            "loss": player_data.get("loss", 0),
            "access": access,
        })
    return info
