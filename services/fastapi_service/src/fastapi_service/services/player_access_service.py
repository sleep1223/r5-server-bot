from __future__ import annotations

import ipaddress
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, cast

from loguru import logger
from shared_lib.models import BanRecord, Player, PlayerAccessNotice, PlayerAccessOperation, PlayerAccessRule, Server
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from fastapi_service.core.cache import server_cache
from fastapi_service.core.utils import CN_TZ, generate_hash, resolve_ips_batch

GEO_POLICY_RULE_TYPE = "geo_policy"
GEO_POLICY_RULE_VALUE = "mainland_boundary"
GEO_POLICY_GLOBAL_RULE_ID = "server_geo_policy:global"
GEO_POLICY_FOREIGN_TO_DOMESTIC_REASON = "REGION_LOCK_TO_HK"
GEO_POLICY_DOMESTIC_TO_OVERSEAS_REASON = "REGION_LOCK_TO_MAINLAND"
RULE_TYPES = {"uid", "ip", "cidr", "geo", "country", "region", GEO_POLICY_RULE_TYPE}
RULE_ACTIONS = {"allow", "deny"}
SERVER_SCOPES = {"global", "server"}
DEFAULT_REASON_LOCALE = "zh"
SELF_UNBAN_URL = "r5.sleep0.de/bans"
REGION_LOCK_REASON = "REGION_LOCK"
REASON_TEXTS = {
    "zh": {
        "NO_COVER": "撤回掩体",
        "BE_POLITE": "言行不当",
        "CHEAT": "作弊",
        "RULES": "违反规则",
    },
    "en": {
        "NO_COVER": "No-cover rule violation",
        "BE_POLITE": "Inappropriate behavior",
        "CHEAT": "Cheating",
        "RULES": "Rule violation",
    },
    "ja": {
        "NO_COVER": "遮蔽物の撤去違反",
        "BE_POLITE": "不適切な言動",
        "CHEAT": "チート行為",
        "RULES": "ルール違反",
    },
    "ko": {
        "NO_COVER": "엄폐물 제거 규칙 위반",
        "BE_POLITE": "부적절한 언행",
        "CHEAT": "부정행위",
        "RULES": "규칙 위반",
    },
}
GEO_POLICY_REASON_TEXTS = {
    "zh": {
        REGION_LOCK_REASON: "您的网络延迟过高，请选择延迟更低的服务器",
        GEO_POLICY_FOREIGN_TO_DOMESTIC_REASON: "您的网络延迟过高，请前往香港服务器游玩",
        GEO_POLICY_DOMESTIC_TO_OVERSEAS_REASON: "您的网络延迟过高，请选择国内服务器游玩",
    },
    "en": {
        REGION_LOCK_REASON: "Your latency is too high. Please choose a lower-latency server",
        GEO_POLICY_FOREIGN_TO_DOMESTIC_REASON: "Your latency is too high. Please play on a Hong Kong server",
        GEO_POLICY_DOMESTIC_TO_OVERSEAS_REASON: "Your latency is too high. Please play on a mainland China server",
    },
    "ja": {
        REGION_LOCK_REASON: "通信遅延が高すぎます。より低遅延のサーバーを選択してください",
        GEO_POLICY_FOREIGN_TO_DOMESTIC_REASON: "通信遅延が高すぎます。香港サーバーでプレイしてください",
        GEO_POLICY_DOMESTIC_TO_OVERSEAS_REASON: "通信遅延が高すぎます。中国国内サーバーでプレイしてください",
    },
    "ko": {
        REGION_LOCK_REASON: "네트워크 지연 시간이 너무 높습니다. 지연 시간이 더 낮은 서버를 선택해 주세요",
        GEO_POLICY_FOREIGN_TO_DOMESTIC_REASON: "네트워크 지연 시간이 너무 높습니다. 홍콩 서버에서 플레이해 주세요",
        GEO_POLICY_DOMESTIC_TO_OVERSEAS_REASON: "네트워크 지연 시간이 너무 높습니다. 중국 본토 서버를 선택해 주세요",
    },
}
ACTION_REASON_PREFIXES = {
    "zh": {
        "ban": "已被封禁",
        "kick": "已被踢出",
    },
    "en": {
        "ban": "Banned",
        "kick": "Kicked",
    },
    "ja": {
        "ban": "参加禁止",
        "kick": "キック",
    },
    "ko": {
        "ban": "차단됨",
        "kick": "퇴장됨",
    },
}
ACTION_DEFAULT_REASONS = {
    "zh": {
        "ban": "禁止进入该服务器",
        "kick": "请访问网站确认后再进入服务器",
    },
    "en": {
        "ban": "You are banned from this server",
        "kick": "Please visit the website to confirm before joining this server",
    },
    "ja": {
        "ban": "このサーバーへの参加は禁止されています",
        "kick": "参加する前にウェブサイトで確認してください",
    },
    "ko": {
        "ban": "이 서버에 참가할 수 없습니다",
        "kick": "서버에 참가하기 전에 웹사이트에서 확인해 주세요",
    },
}
SELF_UNBAN_GUIDES = {
    "zh": f"请前往 {SELF_UNBAN_URL} 自助解封",
    "en": f"Visit {SELF_UNBAN_URL} to self-unban",
    "ja": f"セルフ解除は {SELF_UNBAN_URL} にアクセスしてください",
    "ko": f"셀프 해제는 {SELF_UNBAN_URL} 에서 할 수 있습니다",
}
SDK_ONLINE_ACTION_DEFAULT_REASONS = {
    "ban": "Banned by server policy",
    "kick": "Kicked by server policy",
}


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
        address = ipaddress.ip_address(text)
        return str(address.ipv4_mapped or address) if isinstance(address, ipaddress.IPv6Address) else str(address)
    except ValueError:
        return text


def _normalize_server_identity_ip(ip: object | None) -> str:
    host = _normalize_ip(ip)
    if not host:
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.is_link_local or address.is_unspecified:
        return ""
    return host


def _safe_int(value: object | None, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _append_unique_text(items: list[str], value: object | None) -> None:
    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)


def _server_address_key(server_ip: object | None, server_port: object | None) -> str | None:
    host = _normalize_server_identity_ip(server_ip) or None
    port = _safe_int(server_port, 0)
    if not host or not port:
        return None
    return f"{host}:{port}"


def _normalize_server_keys(
    server_id: object | None = None,
    server_keys: Iterable[object] | object | None = None,
) -> list[str]:
    keys: list[str] = []
    _append_unique_text(keys, server_id)
    if server_keys is None:
        return keys

    if isinstance(server_keys, str):
        _append_unique_text(keys, server_keys)
        return keys

    try:
        iterator = iter(server_keys)  # type: ignore[arg-type]
    except TypeError:
        _append_unique_text(keys, server_keys)
        return keys

    for key in iterator:
        _append_unique_text(keys, key)
    return keys


def _input_device_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("inputDevice", "input_device", "input", "device"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text[:50]
    return None


def _split_reason_token(reason: str) -> tuple[str | None, str | None]:
    for action, prefix in (("ban", "#BAN_REASON_"), ("kick", "#KICK_REASON_")):
        if reason.startswith(prefix):
            return action, reason[len(prefix) :]
    return None, None


def _normalize_reason_locale(locale: str | None) -> str:
    normalized = str(locale or DEFAULT_REASON_LOCALE).strip().lower().replace("_", "-")
    if normalized.startswith("zh"):
        return "zh"
    if normalized.startswith("ja") or normalized.startswith("jp"):
        return "ja"
    if normalized.startswith("ko") or normalized.startswith("kr"):
        return "ko"
    if normalized.startswith("en"):
        return "en"
    return DEFAULT_REASON_LOCALE


def reason_locale_from_geo(country: str | None, region: str | None = None) -> str:
    country_text = str(country or "").strip().lower()
    region_text = str(region or "").strip().lower()
    geo_text = f"{country_text} {region_text}"

    if any(marker in geo_text for marker in ("日本", "japan", " jp")) or country_text == "jp":
        return "ja"
    if any(marker in geo_text for marker in ("韩国", "韓國", "한국", "대한민국", "korea", "south korea")) or country_text == "kr":
        return "ko"
    if any(marker in geo_text for marker in ("中国", "china", "hong kong", "macau", "taiwan", "香港", "澳门", "澳門", "台湾", "台灣")):
        return "zh"
    if country_text in {"cn", "hk", "mo", "tw"}:
        return "zh"
    return "en" if country_text or region_text else DEFAULT_REASON_LOCALE


def _is_mainland_china_geo(country: str | None, region: str | None = None) -> bool | None:
    country_text = str(country or "").strip().lower()
    region_text = str(region or "").strip().lower()
    geo_text = f"{country_text} {region_text}"

    if not country_text and not region_text:
        return None

    non_mainland_markers = ("hong kong", "macau", "macao", "taiwan", "香港", "澳门", "澳門", "台湾", "台灣")
    if any(marker in geo_text for marker in non_mainland_markers) or country_text in {"hk", "mo", "tw"}:
        return False

    if country_text in {"cn", "china"} or "中国" in country_text:
        return True

    return False


def action_reason_text(action: str | None, reason: str | None, *, locale: str = DEFAULT_REASON_LOCALE) -> str:
    reason_locale = _normalize_reason_locale(locale)
    normalized_action = str(action or "").strip().lower()
    text = str(reason or "").strip()
    default_reasons = ACTION_DEFAULT_REASONS[reason_locale]

    if not text:
        return default_reasons.get(normalized_action, default_reasons["ban"])

    token_action, token_reason = _split_reason_token(text)
    if token_reason:
        normalized_action = normalized_action or (token_action or "")
        text = token_reason
    elif text.lower() == "banned" and normalized_action == "ban":
        return default_reasons["ban"]
    elif text in GEO_POLICY_REASON_TEXTS[reason_locale]:
        return geo_policy_reason_text(text, locale=reason_locale)
    elif text not in REASON_TEXTS[reason_locale]:
        return text

    reason_text = REASON_TEXTS[reason_locale].get(text, text)
    prefix = ACTION_REASON_PREFIXES[reason_locale].get(normalized_action)
    return f"{prefix}: {reason_text}" if prefix else reason_text


def geo_policy_reason_text(reason: str | None, *, locale: str = DEFAULT_REASON_LOCALE) -> str:
    reason_locale = _normalize_reason_locale(locale)
    text = str(reason or "").strip() or REGION_LOCK_REASON
    return GEO_POLICY_REASON_TEXTS[reason_locale].get(text, text)


def _with_self_unban_guide(reason: str, *, locale: str = DEFAULT_REASON_LOCALE) -> str:
    if SELF_UNBAN_URL in reason:
        return reason
    reason_locale = _normalize_reason_locale(locale)
    guide = SELF_UNBAN_GUIDES[reason_locale]
    separator = ". " if reason_locale in {"en", "ja", "ko"} else "。"
    return f"{reason}{separator}{guide}"


def _sdk_online_action_reason(action: str | None, reason: object | None) -> str:
    normalized_action = str(action or "").strip().lower()
    text = str(reason or "").strip()
    if text and all(char.isprintable() for char in text):
        return text
    return SDK_ONLINE_ACTION_DEFAULT_REASONS.get(normalized_action, "Access denied by server policy")


async def upsert_sdk_server_snapshot(
    *,
    server_id: object | None,
    server_ip: object | None = None,
    server_port: object | None = None,
    server_name: object | None = None,
    map_name: object | None = None,
    num_players: object | None = None,
    max_players: object | None = None,
    has_status: bool = False,
) -> Server | None:
    host = _normalize_server_identity_ip(server_ip) or None
    port = _safe_int(server_port, 0) or 37015
    reported_name = str(server_name or "").strip() or None
    fallback_name = f"{host}:{port}" if host and port else None

    if not host and not reported_name:
        return None

    server = await Server.filter(host=host, port=port).first() if host else None
    if server is None and host:
        host_matches = await Server.filter(host=host).limit(2).all()
        if len(host_matches) == 1:
            server = host_matches[0]
    if server is None and reported_name:
        name_matches = await Server.filter(name__iexact=reported_name).limit(2).all()
        if len(name_matches) == 1:
            server = name_matches[0]

    now = datetime.now(timezone.utc)
    if server is None:
        if not host:
            return None
        try:
            server = await Server.create(
                server_id=None,
                host=host,
                port=port,
                name=reported_name or fallback_name or f"server-{host}",
                map=str(map_name or "") or None,
                player_count=_safe_int(num_players, 0),
                max_players=_safe_int(max_players, 0),
                is_self_hosted=True,
                has_status=has_status,
                last_seen_at=now,
            )
        except IntegrityError:
            return await upsert_sdk_server_snapshot(
                server_id=None,
                server_ip=host,
                server_port=port,
                server_name=reported_name,
                map_name=map_name,
                num_players=num_players,
                max_players=max_players,
                has_status=has_status,
            )
        return server

    updates: dict[str, Any] = {"last_seen_at": now}
    if host and server.host != host:
        updates["host"] = host
    if port and server.port != port:
        updates["port"] = port
    display_name = reported_name or fallback_name
    if display_name and server.name != display_name:
        updates["name"] = display_name
    if map_name is not None:
        updates["map"] = str(map_name or "") or None
    if num_players is not None:
        updates["player_count"] = _safe_int(num_players, 0)
    if max_players is not None:
        updates["max_players"] = _safe_int(max_players, 0)
    if not server.is_self_hosted:
        updates["is_self_hosted"] = True
    if has_status and not server.has_status:
        updates["has_status"] = True

    await Server.filter(id=server.id).update(**updates)
    for key, value in updates.items():
        setattr(server, key, value)
    return server


async def resolve_access_server_identity(
    *,
    server_id: object | None,
    server_ip: object | None = None,
    server_port: object | None = None,
    server_name: object | None = None,
    map_name: object | None = None,
    num_players: object | None = None,
    max_players: object | None = None,
    has_status: bool = False,
) -> dict[str, Any]:
    server = await upsert_sdk_server_snapshot(
        server_id=server_id,
        server_ip=server_ip,
        server_port=server_port,
        server_name=server_name,
        map_name=map_name,
        num_players=num_players,
        max_players=max_players,
        has_status=has_status,
    )

    reported_host = _normalize_server_identity_ip(server_ip) or None
    reported_port = _safe_int(server_port, 0) or None
    reported_address_key = _server_address_key(reported_host, reported_port)
    reported_name = str(server_name or "").strip() or None
    reported_name_key = f"name:{reported_name.casefold()}" if reported_name else None

    keys: list[str] = []
    _append_unique_text(keys, reported_address_key)
    _append_unique_text(keys, reported_host)
    _append_unique_text(keys, reported_name_key)

    if server is not None:
        _append_unique_text(keys, _server_address_key(server.host, server.port))
        _append_unique_text(keys, server.host)

    canonical_key = _server_address_key(server.host, server.port) if server is not None else reported_address_key or reported_name_key
    return {
        "server": server,
        "server_keys": keys,
        "canonical_key": canonical_key,
    }


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _ban_rule_id(uid: str) -> str:
    return f"legacy_ban:{uid}"


def _scoped_ban_rule_id(uid: str, server_id: str | None) -> str:
    if server_id:
        return f"server_ban:{server_id}:{uid}"
    return _ban_rule_id(uid)


def _ban_reason(reason: str | None, *, locale: str = DEFAULT_REASON_LOCALE) -> str:
    return action_reason_text("ban", reason, locale=locale)


def _kick_reason(reason: str | None, *, locale: str = DEFAULT_REASON_LOCALE) -> str:
    return action_reason_text("kick", reason, locale=locale)


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


def _scope_filter(
    server_id: object | None = None,
    *,
    server_keys: Iterable[object] | object | None = None,
) -> Q:
    normalized_server_keys = _normalize_server_keys(server_id, server_keys)
    if normalized_server_keys:
        return Q(server_scope="global") | Q(server_scope="server", server_id__in=normalized_server_keys)
    return Q(server_scope="global")


def _not_expired_rule_filter() -> Q:
    now = _now()
    return Q(expires_at__isnull=True) | Q(expires_at__gt=now)


def _active_rule_filter() -> Q:
    return Q(enabled=True) & _not_expired_rule_filter()


def _scope_sort_key(
    rule: PlayerAccessRule,
    server_id: object | None = None,
    *,
    server_keys: Iterable[object] | object | None = None,
) -> tuple[int, int, int, int]:
    normalized_server_keys = _normalize_server_keys(server_id, server_keys)
    key_rank = {key: index for index, key in enumerate(normalized_server_keys)}
    matched_rank = key_rank.get(str(rule.server_id or "").strip(), len(key_rank))
    scope_rank = 0 if rule.server_scope == "server" and matched_rank < len(key_rank) else 1
    return scope_rank, rule.priority, matched_rank, rule.id


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


def _rule_decision(rule: PlayerAccessRule, *, locale: str = DEFAULT_REASON_LOCALE) -> dict[str, Any]:
    reason_locale = _normalize_reason_locale(locale)
    allow = rule.action == "allow"
    return {
        "allow": allow,
        "reason": None if allow else action_reason_text(rule.source_action, rule.reason, locale=reason_locale),
        "reason_locale": reason_locale,
        "rule_id": rule.rule_id or f"access_rule:{rule.id}",
        "rule_type": rule.rule_type,
        "server_scope": rule.server_scope,
        "server_id": rule.server_id,
        "rule": serialize_access_rule(rule),
        "source": "player_access_rule",
    }


def _legacy_ban_decision(uid: str, reason: str | None, operator: str | None, *, locale: str = DEFAULT_REASON_LOCALE) -> dict[str, Any]:
    reason_locale = _normalize_reason_locale(locale)
    return {
        "allow": False,
        "reason": _ban_reason(reason, locale=reason_locale),
        "reason_locale": reason_locale,
        "rule_id": _ban_rule_id(uid),
        "rule_type": "uid",
        "server_scope": "global",
        "server_id": None,
        "source": "legacy_ban",
        "operator": operator,
    }


def _notice_decision(notice: PlayerAccessNotice, *, locale: str = DEFAULT_REASON_LOCALE) -> dict[str, Any]:
    reason_locale = _normalize_reason_locale(locale)
    reason = action_reason_text(notice.action, notice.reason or notice.message, locale=reason_locale)
    if str(notice.action or "").strip().lower() == "kick" and notice.requires_ack:
        reason = _with_self_unban_guide(reason, locale=reason_locale)
    return {
        "allow": False,
        "reason": reason,
        "reason_locale": reason_locale,
        "rule_id": f"kick_notice:{notice.id}",
        "rule_type": "notice",
        "server_scope": notice.server_scope,
        "server_id": notice.server_id,
        "source": "kick_notice",
        "notice": serialize_access_notice(notice),
    }


def action_from_access_decision(decision: dict[str, Any]) -> Literal["kick", "ban"] | None:
    if decision.get("allow", True):
        return None
    if decision.get("source") == "kick_notice":
        return "kick"
    if decision.get("source") == "legacy_ban":
        return "ban"

    rule = decision.get("rule") or {}
    source_action = str(rule.get("source_action") or "").strip().lower()
    if source_action == "kick":
        return "kick"
    if source_action == "ban":
        return "ban"

    return "ban"


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
    server_keys: Iterable[object] | object | None = None,
) -> PlayerAccessRule | None:
    try:
        rules = await PlayerAccessRule.filter(
            _scope_filter(server_id, server_keys=server_keys),
            _active_rule_filter(),
            rule_type=rule_type,
            action=action,
            value=value,
        ).order_by("priority", "id")
        if not rules:
            return None
        return sorted(rules, key=lambda r: _scope_sort_key(r, server_id, server_keys=server_keys))[0]
    except Exception as exc:
        logger.warning(f"玩家准入精确规则查询失败: {exc}")
        return None


async def _matching_cidr_rule(
    action: str,
    ip: str,
    *,
    server_id: object | None = None,
    server_keys: Iterable[object] | object | None = None,
) -> PlayerAccessRule | None:
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None

    try:
        rules = await PlayerAccessRule.filter(
            _scope_filter(server_id, server_keys=server_keys),
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
    return sorted(matches, key=lambda r: _scope_sort_key(r, server_id, server_keys=server_keys))[0]


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


def _snapshot_ip(context: dict[str, Any] | None) -> str:
    if not isinstance(context, dict):
        return ""
    return _normalize_ip(context.get("player_ip") or context.get("ip"))


def _snapshot_geo(context: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not isinstance(context, dict):
        return None, None
    country = context.get("player_country") or context.get("country")
    region = context.get("player_region") or context.get("region")
    return (str(country).strip() or None) if country is not None else None, (str(region).strip() or None) if region is not None else None


async def _snapshot_locale_result(context: dict[str, Any] | None, *, fallback: str) -> tuple[str, bool]:
    country, region = _snapshot_geo(context)
    ip = _snapshot_ip(context)
    if not (country or region) and ip:
        country, region = await _resolve_geo(ip)
    if country or region:
        return reason_locale_from_geo(country, region), True
    return fallback, False


async def _snapshot_locale(context: dict[str, Any] | None, *, fallback: str) -> str:
    locale, _ = await _snapshot_locale_result(context, fallback=fallback)
    return locale


async def _operation_result_for_id(operation_id: object | None) -> dict[str, Any] | None:
    if not operation_id:
        return None
    operation = await PlayerAccessOperation.get_or_none(id=operation_id)
    return operation.result if operation and isinstance(operation.result, dict) else None


async def _notice_locale(notice: PlayerAccessNotice, *, fallback: str) -> str:
    locale, found = await _snapshot_locale_result(notice.message_context if isinstance(notice.message_context, dict) else None, fallback=fallback)
    if found:
        return locale
    return await _snapshot_locale(await _operation_result_for_id(getattr(notice, "operation_id", None)), fallback=fallback)


async def _rule_locale(rule: PlayerAccessRule, *, fallback: str) -> str:
    return await _snapshot_locale(await _operation_result_for_id(getattr(rule, "source_operation_id", None)), fallback=fallback)


async def _resolve_server_geo(identity: dict[str, Any], server_ip: object | None = None) -> tuple[str | None, str | None]:
    server = identity.get("server")
    candidate_ip = _normalize_server_identity_ip(server_ip) or _normalize_server_identity_ip(getattr(server, "host", None))
    if candidate_ip:
        country, region = await _resolve_geo(candidate_ip)
        if country or region:
            return country, region

    server_region = str(getattr(server, "region", "") or "").strip()
    if server_region:
        return server_region, None
    return None, None


async def _ensure_default_server_geo_policy_rule() -> None:
    try:
        existing = await PlayerAccessRule.filter(
            rule_type=GEO_POLICY_RULE_TYPE,
            action="deny",
            value=GEO_POLICY_RULE_VALUE,
            server_scope="global",
        ).first()
        if existing:
            return

        await PlayerAccessRule.create(
            rule_type=GEO_POLICY_RULE_TYPE,
            action="deny",
            value=GEO_POLICY_RULE_VALUE,
            server_scope="global",
            server_id=None,
            reason=REGION_LOCK_REASON,
            rule_id=GEO_POLICY_GLOBAL_RULE_ID,
            source_action="kick",
            enabled=False,
            priority=100,
        )
    except IntegrityError:
        return
    except Exception as exc:
        logger.warning(f"默认服务器地区准入规则初始化失败: {exc}")


async def _server_geo_policy_config_rule(
    server_id: object | None = None,
    *,
    server_keys: Iterable[object] | object | None = None,
) -> PlayerAccessRule | None:
    await _ensure_default_server_geo_policy_rule()

    normalized_server_keys = _normalize_server_keys(server_id, server_keys)
    scope_q = Q(server_scope="global")
    if normalized_server_keys:
        scope_q |= Q(server_scope="server", server_id__in=normalized_server_keys)

    try:
        rules = await PlayerAccessRule.filter(
            scope_q,
            _not_expired_rule_filter(),
            rule_type=GEO_POLICY_RULE_TYPE,
            action="deny",
            value=GEO_POLICY_RULE_VALUE,
        ).order_by("priority", "id")
    except Exception as exc:
        logger.warning(f"服务器地区准入规则查询失败: {exc}")
        return None

    if not rules:
        return None

    server_rules = [rule for rule in rules if rule.server_scope == "server"]
    if server_rules:
        return sorted(server_rules, key=lambda r: _scope_sort_key(r, server_id, server_keys=server_keys))[0]

    global_rules = [rule for rule in rules if rule.server_scope == "global"]
    if global_rules:
        return sorted(global_rules, key=lambda r: _scope_sort_key(r, server_id, server_keys=server_keys))[0]
    return None


def _server_geo_policy_decision(
    *,
    player_country: str | None,
    player_region: str | None,
    server_country: str | None,
    server_region: str | None,
    config_rule: PlayerAccessRule | None,
    reason_locale: str = DEFAULT_REASON_LOCALE,
) -> dict[str, Any] | None:
    if not config_rule or not config_rule.enabled:
        return None

    player_is_mainland = _is_mainland_china_geo(player_country, player_region)
    server_is_mainland = _is_mainland_china_geo(server_country, server_region)
    if player_is_mainland is None or server_is_mainland is None:
        return None
    if player_is_mainland == server_is_mainland:
        return None

    direction = "domestic_server_foreign_player" if server_is_mainland else "overseas_server_domestic_player"
    reason_locale = _normalize_reason_locale(reason_locale)
    source_action = str(config_rule.source_action or "").strip().lower()
    if source_action not in {"ban", "kick"}:
        source_action = "kick"
    reason = str(config_rule.reason or "").strip() or REGION_LOCK_REASON
    if reason == REGION_LOCK_REASON:
        reason = GEO_POLICY_FOREIGN_TO_DOMESTIC_REASON if direction == "domestic_server_foreign_player" else GEO_POLICY_DOMESTIC_TO_OVERSEAS_REASON
    rule = serialize_access_rule(config_rule)
    rule.update({
        "source_action": source_action,
        "matched_policy": direction,
        "player_country": player_country,
        "player_region": player_region,
        "server_country": server_country,
        "server_region": server_region,
    })
    return {
        "allow": False,
        "reason": geo_policy_reason_text(reason, locale=reason_locale),
        "reason_locale": reason_locale,
        "rule_id": config_rule.rule_id or f"access_rule:{config_rule.id}",
        "rule_type": config_rule.rule_type,
        "server_scope": config_rule.server_scope,
        "server_id": config_rule.server_id,
        "rule": rule,
        "source": "server_geo_policy",
    }


async def _matching_geo_deny_rule(
    *,
    ip: str,
    country: str | None,
    region: str | None,
    server_id: object | None = None,
    server_keys: Iterable[object] | object | None = None,
) -> PlayerAccessRule | None:
    try:
        rules = await PlayerAccessRule.filter(
            _scope_filter(server_id, server_keys=server_keys),
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
    return sorted(matches, key=lambda r: _scope_sort_key(r, server_id, server_keys=server_keys))[0]


async def _legacy_ban_for_uid(uid: str, player: Player | None, *, locale: str = DEFAULT_REASON_LOCALE) -> dict[str, Any] | None:
    player = player or await _find_player_for_uid(uid)
    if not player or player.status != "banned":
        return None

    record = await BanRecord.filter(player=player).order_by("-created_at").first()
    if record is None:
        return _legacy_ban_decision(uid, "banned", None, locale=locale)
    return _legacy_ban_decision(uid, record.reason, record.operator, locale=locale)


async def _pending_notice_for_uid(
    uid: str,
    server_id: object | None = None,
    *,
    server_keys: Iterable[object] | object | None = None,
) -> PlayerAccessNotice | None:
    if not uid:
        return None

    try:
        notices = await PlayerAccessNotice.filter(
            _scope_filter(server_id, server_keys=server_keys),
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

    normalized_server_keys = _normalize_server_keys(server_id, server_keys)
    key_rank = {key: index for index, key in enumerate(normalized_server_keys)}

    def _notice_sort_key(notice: PlayerAccessNotice) -> tuple[int, int, int]:
        matched_rank = key_rank.get(str(notice.server_id or "").strip(), len(key_rank))
        scope_rank = 0 if notice.server_scope == "server" and matched_rank < len(key_rank) else 1
        return scope_rank, matched_rank, -notice.id

    return sorted(active_notices, key=_notice_sort_key)[0]


async def evaluate_player_access(
    *,
    uid: object | None,
    ip: object | None = None,
    server_id: object | None = None,
    server_keys: Iterable[object] | object | None = None,
    player: Player | None = None,
    country: str | None = None,
    region: str | None = None,
    server_country: str | None = None,
    server_region: str | None = None,
    reason_locale: str = DEFAULT_REASON_LOCALE,
) -> dict[str, Any]:
    """Evaluate access using the SDK document's fixed priority order."""
    uid_text = normalize_uid(uid)
    ip_text = _normalize_ip(ip)
    locale = _normalize_reason_locale(reason_locale)

    if uid_text:
        notice = await _pending_notice_for_uid(uid_text, server_id=server_id, server_keys=server_keys)
        if notice:
            return _notice_decision(notice, locale=await _notice_locale(notice, fallback=locale))

        rule = await _first_exact_rule("uid", "allow", uid_text, server_id=server_id, server_keys=server_keys)
        if rule:
            return _rule_decision(rule, locale=locale)

        rule = await _first_exact_rule("uid", "deny", uid_text, server_id=server_id, server_keys=server_keys)
        if rule:
            return _rule_decision(rule, locale=await _rule_locale(rule, fallback=locale))

        legacy_ban = await _legacy_ban_for_uid(uid_text, player, locale=locale)
        if legacy_ban:
            return legacy_ban

    if ip_text:
        rule = await _first_exact_rule(
            "ip",
            "allow",
            ip_text,
            server_id=server_id,
            server_keys=server_keys,
        ) or await _matching_cidr_rule(
            "allow",
            ip_text,
            server_id=server_id,
            server_keys=server_keys,
        )
        if rule:
            return _rule_decision(rule, locale=locale)

        rule = await _first_exact_rule("ip", "deny", ip_text, server_id=server_id, server_keys=server_keys) or await _matching_cidr_rule("deny", ip_text, server_id=server_id, server_keys=server_keys)
        if rule:
            return _rule_decision(rule, locale=locale)

    rule = await _matching_geo_deny_rule(ip=ip_text, country=country, region=region, server_id=server_id, server_keys=server_keys)
    if rule:
        return _rule_decision(rule, locale=locale)

    if server_country or server_region:
        policy_rule = await _server_geo_policy_config_rule(server_id, server_keys=server_keys)
        policy_decision = _server_geo_policy_decision(
            player_country=country,
            player_region=region,
            server_country=server_country,
            server_region=server_region,
            config_rule=policy_rule,
            reason_locale=locale,
        )
        if policy_decision:
            return policy_decision

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
    server_keys: Iterable[object] | object | None = None,
    player: Player | None = None,
    country: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    uid_text = normalize_uid(uid)
    ip_text = _normalize_ip(ip)
    checks: list[dict[str, Any]] = []

    if uid_text:
        notice = await _pending_notice_for_uid(uid_text, server_id=server_id, server_keys=server_keys)
        checks.append(_trace_record("kick_notice", notice is not None, notice))
        if notice:
            decision = _notice_decision(notice, locale=await _notice_locale(notice, fallback=reason_locale_from_geo(country, region)))
            return {"decision": decision, "checks": checks, "matched_rules": [decision]}

        rule = await _first_exact_rule("uid", "allow", uid_text, server_id=server_id, server_keys=server_keys)
        checks.append(_trace_record("uid_allow", rule is not None, rule))
        if rule:
            decision = _rule_decision(rule)
            return {"decision": decision, "checks": checks, "matched_rules": [decision]}

        rule = await _first_exact_rule("uid", "deny", uid_text, server_id=server_id, server_keys=server_keys)
        checks.append(_trace_record("uid_deny", rule is not None, rule))
        if rule:
            decision = _rule_decision(rule, locale=await _rule_locale(rule, fallback=reason_locale_from_geo(country, region)))
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
        rule = await _first_exact_rule(
            "ip",
            "allow",
            ip_text,
            server_id=server_id,
            server_keys=server_keys,
        ) or await _matching_cidr_rule(
            "allow",
            ip_text,
            server_id=server_id,
            server_keys=server_keys,
        )
        checks.append(_trace_record("ip_cidr_allow", rule is not None, rule))
        if rule:
            decision = _rule_decision(rule)
            return {"decision": decision, "checks": checks, "matched_rules": [decision]}

        rule = await _first_exact_rule("ip", "deny", ip_text, server_id=server_id, server_keys=server_keys) or await _matching_cidr_rule("deny", ip_text, server_id=server_id, server_keys=server_keys)
        checks.append(_trace_record("ip_cidr_deny", rule is not None, rule))
        if rule:
            decision = _rule_decision(rule)
            return {"decision": decision, "checks": checks, "matched_rules": [decision]}
    else:
        checks.append(_trace_record("ip", False, note="ip_missing"))

    rule = await _matching_geo_deny_rule(ip=ip_text, country=country, region=region, server_id=server_id, server_keys=server_keys)
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
    server_ip: object | None = None,
    server_port: object | None = None,
    server_name: object | None = None,
) -> dict[str, Any]:
    identity = await resolve_access_server_identity(
        server_id=server_id,
        server_ip=server_ip,
        server_port=server_port,
        server_name=server_name,
        has_status=False,
    )
    server_keys = identity["server_keys"]
    scoped_server_id = identity["canonical_key"]
    player = await upsert_access_player_snapshot(
        uid=uid,
        nucleus_id=nucleus_id,
        player_name=player_name,
        ip=ip,
    )
    normalized_uid = normalize_uid(uid, nucleus_id)
    country = player.country if player else None
    region = player.region if player else None
    reason_locale = reason_locale_from_geo(country, region)
    server_country, server_region = await _resolve_server_geo(identity, server_ip)
    decision = await evaluate_player_access(
        uid=normalized_uid,
        ip=ip,
        server_id=scoped_server_id,
        server_keys=server_keys,
        player=player,
        country=country,
        region=region,
        server_country=server_country,
        server_region=server_region,
        reason_locale=reason_locale,
    )
    action = action_from_access_decision(decision)
    log_message = (
        "单个玩家准入检测: "
        f"allow={bool(decision.get('allow'))}, "
        f"uid={normalized_uid or '-'}, "
        f"player={player_name or (player.name if player else '-')}, "
        f"ip={_normalize_ip(ip) or '-'}, "
        f"port={port or '-'}, "
        f"server_id={server_id or '-'}, "
        f"scope_server_id={scoped_server_id or '-'}, "
        f"server_keys={server_keys or '-'}, "
        f"player_geo={country or '-'}/{region or '-'}, "
        f"server_geo={server_country or '-'}/{server_region or '-'}, "
        f"reason_locale={reason_locale}, "
        f"action={action or '-'}, "
        f"rule_id={decision.get('rule_id') or '-'}, "
        f"reason={decision.get('reason') or '-'}"
    )
    if decision.get("allow"):
        logger.info(log_message)
    else:
        blocked_rule = decision.get("rule") or decision.get("notice")
        if not blocked_rule:
            blocked_rule = {
                "source": decision.get("source"),
                "rule_id": decision.get("rule_id"),
                "rule_type": decision.get("rule_type"),
                "server_scope": decision.get("server_scope"),
                "server_id": decision.get("server_id"),
                "operator": decision.get("operator"),
            }
        logger.error(f"{log_message}, 拦截规则={json.dumps(blocked_rule, ensure_ascii=False, default=str, sort_keys=True)}")
    return decision


async def process_online_players_report(
    *,
    server_id: str,
    report: dict[str, Any],
) -> dict[str, Any]:
    identity = await resolve_access_server_identity(
        server_id=server_id,
        server_ip=report.get("serverIp"),
        server_port=report.get("serverPort"),
        server_name=report.get("serverName") or report.get("hostname"),
        map_name=report.get("map"),
        num_players=report.get("numPlayers"),
        max_players=report.get("maxPlayers"),
        has_status=True,
    )
    server_keys = identity["server_keys"]
    cache_server_id = identity["canonical_key"] or "unknown"
    scoped_server_id = identity["canonical_key"]
    server_country, server_region = await _resolve_server_geo(identity, report.get("serverIp"))

    reported_players = report.get("players") or []
    actions: list[dict[str, Any]] = []
    enriched_players: list[dict[str, Any]] = []
    for player_payload in reported_players:
        if not isinstance(player_payload, dict):
            continue

        uid_text = normalize_uid(player_payload.get("uid"), player_payload.get("nucleusId"))
        if not uid_text:
            continue

        raw_ip = player_payload.get("ip")
        player = await upsert_access_player_snapshot(
            uid=uid_text,
            nucleus_id=player_payload.get("nucleusId"),
            player_name=player_payload.get("playerName"),
            ip=raw_ip,
            input_device=_input_device_from_payload(player_payload),
        )
        country = player.country if player else player_payload.get("country")
        region = player.region if player else player_payload.get("region")
        normalized_ip = _normalize_ip(raw_ip) or (player.ip if player else None)
        enriched_payload = dict(player_payload)
        if normalized_ip:
            enriched_payload["ip"] = normalized_ip
        if country:
            enriched_payload["country"] = country
        if region:
            enriched_payload["region"] = region
        enriched_players.append(enriched_payload)

        reason_locale = reason_locale_from_geo(country, region)
        decision = await evaluate_player_access(
            uid=uid_text,
            ip=normalized_ip or raw_ip,
            server_id=scoped_server_id,
            server_keys=server_keys,
            player=player,
            country=country,
            region=region,
            server_country=server_country,
            server_region=server_region,
            reason_locale=reason_locale,
        )
        action = action_from_access_decision(decision)
        if not action:
            continue

        action_payload: dict[str, Any] = {
            "uid": uid_text,
            "action": action,
            "reason": _sdk_online_action_reason(action, decision.get("reason")),
            "ruleId": decision.get("rule_id"),
        }
        nucleus_id = _uid_to_int(uid_text)
        if nucleus_id is not None:
            action_payload["nucleusId"] = nucleus_id
        actions.append(action_payload)

    enriched_report = dict(report)
    enriched_report["players"] = enriched_players
    server_cache.update_access_report(cache_server_id, enriched_report)

    logger.info(
        "在线玩家上报: "
        f"scope_server_id={scoped_server_id or '-'}, players={len(reported_players)}, "
        f"server_keys={server_keys or '-'}, "
        f"server_geo={server_country or '-'}/{server_region or '-'}, "
        f"actions={len(actions)}, map={report.get('map') or '-'}, "
        f"num={report.get('numPlayers')}/{report.get('maxPlayers')}"
    )
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
            "reason": (reason or "").strip() or None,
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
            ipaddress.ip_address(text)
        except ValueError as exc:
            raise ValueError("value 必须是有效 IP 地址") from exc
        return _normalize_ip(text)

    if rule_type == "cidr":
        try:
            return str(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise ValueError("value 必须是有效 CIDR") from exc

    if rule_type == GEO_POLICY_RULE_TYPE:
        normalized = text.strip().lower()
        if normalized in {"server_geo_policy", "mainland", "mainland_china", "domestic_overseas"}:
            return GEO_POLICY_RULE_VALUE
        if normalized != GEO_POLICY_RULE_VALUE:
            raise ValueError(f"{GEO_POLICY_RULE_TYPE} value 必须是 {GEO_POLICY_RULE_VALUE}")
        return normalized

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
    if normalized_type == GEO_POLICY_RULE_TYPE and normalized_action != "deny":
        raise ValueError(f"{GEO_POLICY_RULE_TYPE} 仅支持 action=deny")

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
    if payload["rule_type"] == GEO_POLICY_RULE_TYPE:
        reason = (reason or "").strip() or REGION_LOCK_REASON
        source_action = (source_action or "").strip() or "kick"
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
    uid_text = normalize_uid(uid)
    action_text = str(action or "").strip().lower()
    if action_text == "kick":
        existing_notice = await (
            PlayerAccessNotice
            .filter(
                uid=uid_text,
                action="kick",
                server_scope=scope,
                server_id=normalized_server_id,
                requires_ack=True,
                acknowledged_at__isnull=True,
            )
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=_now()))
            .order_by("-created_at", "-id")
            .first()
        )
        if existing_notice:
            next_context = dict(message_context or {})
            next_context["pending_notice_reused"] = True
            updates = {
                "player_id": player.id,
                "reason": (reason or "").strip() or None,
                "message": (message or "").strip() or None,
                "message_context": next_context,
                "expires_at": expires_at,
                "operation_id": operation.id if operation else getattr(existing_notice, "operation_id", None),
                "updated_at": _now(),
            }
            await PlayerAccessNotice.filter(id=existing_notice.id).update(**updates)
            for key, value in updates.items():
                setattr(existing_notice, key, value)
            return existing_notice

    return await PlayerAccessNotice.create(
        player_id=player.id,
        uid=uid_text,
        action=action_text,
        reason=(reason or "").strip() or None,
        message=(message or "").strip() or None,
        message_context=message_context,
        server_scope=scope,
        server_id=normalized_server_id,
        requires_ack=True,
        operation_id=operation.id if operation else None,
        expires_at=expires_at,
    )


async def sync_legacy_access_records(*, batch_size: int = 5000) -> dict[str, int]:
    """Backfill old ban/kick state into access-rule/notice tables.

    Older admin paths wrote only players.status / ban_records / kick_count. The
    SDK access pipeline now reads PlayerAccessRule and PlayerAccessNotice first,
    so this keeps historical state visible and enforceable without migrations.
    """
    stats = {
        "banned_checked": 0,
        "ban_rules_created": 0,
        "kick_checked": 0,
        "kick_notices_created": 0,
    }
    batch_size = max(1, batch_size)

    offset = 0
    while True:
        banned_players = await Player.filter(status="banned", nucleus_id__isnull=False).order_by("-updated_at", "-id").offset(offset).limit(batch_size)
        if not banned_players:
            break
        offset += batch_size

        uid_by_player_id = {player.id: uid for player in banned_players if (uid := normalize_uid(player.nucleus_id))}
        existing_rule_values: set[str] = set()
        if uid_by_player_id:
            existing_rule_value_rows = await PlayerAccessRule.filter(
                rule_type="uid",
                action="deny",
                value__in=list(set(uid_by_player_id.values())),
                enabled=True,
            ).values_list("value", flat=True)
            existing_rule_values = set(cast(Iterable[str], existing_rule_value_rows))

        for player in banned_players:
            uid = uid_by_player_id.get(player.id)
            if not uid:
                continue
            stats["banned_checked"] += 1
            if uid in existing_rule_values:
                continue

            record = await BanRecord.filter(player=player).order_by("-created_at", "-id").first()
            rule = await ensure_uid_blacklist_rule(
                player,
                record.reason if record else "RULES",
                record.operator if record else "legacy_sync",
                source_action="ban",
            )
            if rule:
                existing_rule_values.add(uid)
                stats["ban_rules_created"] += 1

    offset = 0
    while True:
        kicked_players = await (
            Player.filter(Q(kick_count__gt=0) | Q(status="kicked"), nucleus_id__isnull=False).exclude(status="banned").order_by("-updated_at", "-id").offset(offset).limit(batch_size)
        )
        if not kicked_players:
            break
        offset += batch_size

        uid_by_player_id = {player.id: uid for player in kicked_players if (uid := normalize_uid(player.nucleus_id))}
        existing_notice_uids: set[str] = set()
        existing_notice_player_ids: set[int] = set()
        if uid_by_player_id:
            notice_rows = await PlayerAccessNotice.filter(Q(uid__in=list(set(uid_by_player_id.values()))) | Q(player_id__in=list(uid_by_player_id.keys()))).values("uid", "player_id")
            existing_notice_uids = {str(row["uid"]) for row in notice_rows if row.get("uid") is not None}
            existing_notice_player_ids = {int(row["player_id"]) for row in notice_rows if row.get("player_id") is not None}

        for player in kicked_players:
            uid = uid_by_player_id.get(player.id)
            if not uid:
                continue
            stats["kick_checked"] += 1
            if uid in existing_notice_uids or player.id in existing_notice_player_ids:
                continue

            await create_access_notice(
                player=player,
                uid=uid,
                action="kick",
                reason="RULES",
                message=_kick_reason("RULES"),
                message_context={
                    "legacy_sync": True,
                    "kick_count": player.kick_count,
                    "player_status": player.status,
                },
                server_scope="global",
                server_id=None,
            )
            existing_notice_uids.add(uid)
            existing_notice_player_ids.add(player.id)
            stats["kick_notices_created"] += 1

    return stats


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
    country = player.country if player else None
    region = player.region if player else None
    return await evaluate_player_access(
        uid=uid_value,
        ip=ip if ip is not None else (player.ip if player else None),
        server_id=server_id,
        player=player,
        country=country,
        region=region,
        reason_locale=reason_locale_from_geo(country, region),
    )


async def build_online_player_info(player_data: dict, *, is_admin: bool = False, server_id: object | None = None) -> dict[str, Any]:
    uid = normalize_uid(player_data.get("uniqueid"))
    player = await _find_player_for_uid(uid) if uid else None
    country = player_data.get("country")
    region = player_data.get("region")
    access = await evaluate_player_access(
        uid=uid,
        ip=player_data.get("ip"),
        server_id=server_id,
        player=player,
        country=country,
        region=region,
        reason_locale=reason_locale_from_geo(country, region),
    )

    uid_int = _uid_to_int(uid)
    info: dict[str, Any] = {
        "name": player_data.get("name", "Unknown"),
        "country": player_data.get("country"),
        "nucleus_id": uid_int if uid_int is not None else (uid or None),
        "input_device": _input_device_from_payload(player_data) or (player.input_device if player else None) or "unknown",
    }
    if is_admin:
        info.update({
            "region": player_data.get("region"),
            "ip": player_data.get("ip"),
            "ping": player_data.get("ping", 0),
            "loss": player_data.get("loss", 0),
            "access": access,
        })
    return info
