import ipaddress

from shared_lib.models import IpInfo, Server
from tortoise.expressions import Q

from fastapi_service.core.cache import server_cache
from fastapi_service.core.utils import parse_short_name
from fastapi_service.services import player_access_service

_SERVER_IDENTIFIER_FIELDS = ("serverId", "server_id", "key", "netkey")


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_identifier(value: object) -> str:
    return str(value or "").strip()


def _raw_server_identifiers(server: dict) -> list[str]:
    identifiers: list[str] = []
    seen: set[str] = set()
    for field in _SERVER_IDENTIFIER_FIELDS:
        identifier = _normalize_identifier(server.get(field))
        if identifier and identifier not in seen:
            identifiers.append(identifier)
            seen.add(identifier)
    return identifiers


def _raw_server_identifier(server: dict) -> str:
    identifiers = _raw_server_identifiers(server)
    return identifiers[0] if identifiers else ""


def _is_cn_raw_server(server: dict, name: str) -> bool:
    region = str(server.get("region") or "").upper()
    return "CN" in name or region in {"CN", "HK", "TW"}


def _strip_public_server_fields(entry: dict) -> None:
    for field in ("ip", "port", "key", "host"):
        entry.pop(field, None)


def _first_positive_int(*values: object) -> int:
    for value in values:
        parsed = _safe_int(value)
        if parsed > 0:
            return parsed
    return 0


def _identity_host_text(value: object | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("["):
        end = text.find("]")
        if end > 0:
            return text[1:end]
    if text.count(":") == 1:
        host_part, _, port_part = text.rpartition(":")
        if port_part.isdigit():
            return host_part
    return text


def _is_unusable_server_identity(value: object | None) -> bool:
    host = _identity_host_text(value)
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_link_local or address.is_unspecified


def _address_key(host: object | None, port: object | None) -> str:
    host_text = str(host or "").strip()
    port_int = _safe_int(port)
    return f"{host_text}:{port_int}" if host_text and port_int else ""


def _name_key(name: object | None) -> str:
    return str(name or "").strip().casefold()


def _status_address_key(status: dict | None) -> str:
    if not status:
        return ""
    return _address_key(status.get("ip"), status.get("port"))


def get_server_info() -> list[dict]:
    return server_cache.get_online_server_statuses()


async def list_servers(
    *,
    server_name: str | None = None,
    simple: bool = False,
    cn_only: bool = False,
    is_admin: bool = False,
) -> list[dict]:
    """统一的服务器列表查询接口。

    - 默认返回远程服务器列表（raw）与 SDK 在线上报状态合并后的结果。
    - ``simple=True`` 返回精简字段，去除在线玩家列表等重字段。
    - ``cn_only=True`` 只返回已经通过 SDK 在线上报命中过的本地服务器
      （即 r5_target_keys 配置命中的中国服），附带玩家详情与延迟。
    - ``server_name`` 对服务器名做不区分大小写的模糊过滤。
    """

    synced_by_key: dict[str, dict] = {}
    synced_by_ip: dict[str, dict] = {}
    synced_by_server_id: dict[str, dict] = {}
    synced_by_name_candidates: dict[str, list[dict]] = {}
    for s_status in server_cache.get_online_server_statuses():
        status_name = s_status.get("_api_name") or s_status.get("hostname")
        if (
            _is_unusable_server_identity(s_status.get("ip"))
            or _is_unusable_server_identity(s_status.get("_server"))
            or _is_unusable_server_identity(status_name)
        ):
            continue

        server_identifier = str(s_status.get("server_id") or "").strip()
        if server_identifier:
            synced_by_server_id[server_identifier] = s_status

        host = str(s_status.get("ip") or "")
        port = s_status.get("port")
        if not host or not port:
            server_str = str(s_status.get("_server") or "")
            if ":" in server_str:
                host_part, _, port_part = server_str.rpartition(":")
                parsed_port = _safe_int(port_part)
                if host_part and parsed_port:
                    host = host or host_part
                    port = port or parsed_port
        status_key = _address_key(host, port)
        if status_key:
            synced_by_key[status_key] = s_status
            synced_by_ip.setdefault(host, s_status)
        status_name_key = _name_key(status_name)
        if status_name_key:
            synced_by_name_candidates.setdefault(status_name_key, []).append(s_status)

    raw = server_cache.raw_response
    raw_list: list[dict] = []
    if isinstance(raw, dict):
        raw_servers = raw.get("servers")
        if isinstance(raw_servers, list):
            raw_list = [s for s in raw_servers if isinstance(s, dict)]

    raw_identifiers: set[str] = set()
    for s in raw_list:
        raw_identifiers.update(_raw_server_identifiers(s))
    raw_hosts = {str(s.get("ip") or "").strip() for s in raw_list if str(s.get("ip") or "").strip()}
    server_filter = Q(has_status=True) | Q(is_self_hosted=True)
    if raw_identifiers:
        server_filter |= Q(server_id__in=list(raw_identifiers)) | Q(netkey__in=list(raw_identifiers))
    if raw_hosts:
        server_filter |= Q(host__in=list(raw_hosts))

    online_server_rows = await Server.filter(server_filter).all()
    db_by_identifier = {s.server_id: s for s in online_server_rows if s.server_id}
    db_by_address = {_address_key(s.host, s.port): s for s in online_server_rows if _address_key(s.host, s.port)}
    db_by_host_candidates: dict[str, list[Server]] = {}
    db_by_name_candidates: dict[str, list[Server]] = {}
    for server_row in online_server_rows:
        if server_row.host:
            db_by_host_candidates.setdefault(server_row.host, []).append(server_row)
        name_key = _name_key(server_row.name)
        if name_key:
            db_by_name_candidates.setdefault(name_key, []).append(server_row)

    def _unique_db_by_host(host: str) -> Server | None:
        candidates = db_by_host_candidates.get(host) or []
        return candidates[0] if len(candidates) == 1 else None

    def _unique_db_by_name(name: object | None) -> Server | None:
        candidates = db_by_name_candidates.get(_name_key(name)) or []
        return candidates[0] if len(candidates) == 1 else None

    def _unique_status_by_name(name: object | None) -> dict | None:
        candidates = synced_by_name_candidates.get(_name_key(name)) or []
        return candidates[0] if len(candidates) == 1 else None

    ping_hosts: set[str] = set(raw_hosts)
    ping_hosts.update(str(status.get("ip") or "").strip() for status in synced_by_key.values() if str(status.get("ip") or "").strip())
    ping_hosts.update(str(s.host or "").strip() for s in online_server_rows if str(s.host or "").strip())
    ip_info_by_ip = {info.ip: info for info in await IpInfo.filter(ip__in=list(ping_hosts)).all()} if ping_hosts else {}

    # cn_only 模式：只返回已同步的本地服务器；raw 列表未出现的也要能返回（避免拉取失败时丢失）
    results: list[dict] = []

    def _match_name(name: str) -> bool:
        if not server_name:
            return True
        return server_name.lower() in (name or "").lower()

    seen_keys: set[str] = set()

    for s in raw_list:
        server_identifiers = _raw_server_identifiers(s)
        server_identifier = server_identifiers[0] if server_identifiers else ""
        ip = str(s.get("ip") or "")
        port = _safe_int(s.get("port"))
        db_server = None
        for identifier in server_identifiers:
            db_server = db_by_identifier.get(identifier)
            if db_server:
                break
        if db_server is None:
            db_server = db_by_address.get(_address_key(ip, port))
        if db_server is None and ip and not port:
            db_server = _unique_db_by_host(ip)
        if db_server:
            if not ip:
                ip = db_server.host
            if not port:
                port = db_server.port

        key = _address_key(ip, port)
        status = None
        for identifier in server_identifiers:
            status = synced_by_server_id.get(identifier)
            if status:
                break
        if status is None:
            status = synced_by_key.get(key) if key else None
        if status is None:
            status = _unique_status_by_name(s.get("name"))
        if not ip and db_server is None and status is None:
            continue
        # raw 缺 port 时按 ip 兜底匹配，避免同一服务器在结果里出现两次
        if status is None and ip and not port:
            status = synced_by_ip.get(ip)
            if status:
                port = _safe_int(status.get("port")) or port
                key = _address_key(ip, port) or ip
        full_name = s.get("name") or (status.get("_api_name") if status else None) or (status.get("hostname") if status else None) or "Unknown"
        if _is_unusable_server_identity(ip) or _is_unusable_server_identity(full_name):
            continue
        is_cn_raw = _is_cn_raw_server(s, full_name)
        has_status = status is not None or db_server is not None or bool(ip)
        if cn_only and not (status is not None or is_cn_raw):
            continue
        if not _match_name(full_name):
            continue

        raw_short_name = parse_short_name(full_name)
        short_name = raw_short_name or (db_server.short_name if db_server else None) or (status.get("short_name") if status else None)
        raw_player_count = _safe_int(s.get("playerCount") or s.get("numPlayers"))
        raw_max_players = _safe_int(s.get("maxPlayers"))
        ip_info = ip_info_by_ip.get(ip)
        status_ip_info = ip_info_by_ip.get(str(status.get("ip") or "").strip()) if status else None
        server_ping = _first_positive_int(
            s.get("ping"),
            status_ip_info.ping if status_ip_info else None,
            ip_info.ping if ip_info else None,
            db_server.ping if db_server else None,
            status.get("server_ping") if status else None,
        )
        display_ip = ip if ip and ip != server_identifier else ""

        entry: dict = {
            "name": full_name,
            "short_name": short_name,
            "full_name": full_name,
            "server_id": server_identifier or None,
            "key": server_identifier or None,
            "ip": display_ip or None,
            "port": port if display_ip else None,
            "region": s.get("region"),
            "map": s.get("map"),
            "playlist": s.get("playlist"),
            "player_count": raw_player_count,
            "max_players": raw_max_players,
            "has_status": has_status,
            "ping": server_ping,
        }

        if status:
            status_key = _status_address_key(status)
            players_data = status.get("players") or []
            # 只有当 SDK 上报真的提供玩家表（即使表里 0 人）时才用上报人数；
            # 解析失败/超时（players_parsed=False）回退到 raw playerCount，避免误报 0
            players_parsed = bool(status.get("players_parsed"))
            entry["player_count"] = len(players_data) if players_parsed else raw_player_count
            entry["max_players"] = status.get("max_players") or raw_max_players
            entry["ping"] = _first_positive_int(server_ping, status.get("server_ping"))
            entry["country"] = status.get("country")
            entry["host"] = status.get("_server")
            if is_admin:
                entry["admin_region"] = status.get("region")

            if not simple:
                player_list = []
                access_server_id = status_key or key or status.get("_server") or status.get("server_id")
                for p in players_data:
                    p_info = await player_access_service.build_online_player_info(
                        p,
                        is_admin=is_admin,
                        server_id=access_server_id,
                    )
                    player_list.append(p_info)
                entry["players"] = player_list
        elif not simple:
            entry["players"] = []

        _strip_public_server_fields(entry)
        if key:
            seen_keys.add(key)
        if status:
            status_key = _status_address_key(status)
            if status_key:
                seen_keys.add(status_key)
        results.append(entry)

    # 补充 raw 列表中缺失但已 SDK 上报到的服务器（例如 raw 拉取失败或服务器离开 master list）
    for key, status in synced_by_key.items():
        if key in seen_keys:
            continue
        status_identifier = str(status.get("server_id") or "").strip()
        host_str = status.get("_server") or key
        host_ip = status.get("ip")
        host_port = status.get("port")
        if not host_ip and ":" in host_str:
            host_ip = host_str.split(":", 1)[0]
        if not host_port and ":" in host_str:
            host_port = _safe_int(host_str.rsplit(":", 1)[-1])
        db_server = db_by_address.get(_address_key(host_ip, host_port))
        if db_server is None and status_identifier:
            identifier_server = db_by_identifier.get(status_identifier)
            if identifier_server and _address_key(identifier_server.host, identifier_server.port) == _address_key(host_ip, host_port):
                db_server = identifier_server
        if db_server is None and host_ip and not host_port:
            db_server = _unique_db_by_host(str(host_ip))
        if db_server is None:
            db_server = _unique_db_by_name(status.get("_api_name") or status.get("hostname"))

        status_name = status.get("_api_name") or status.get("hostname")
        if (
            _is_unusable_server_identity(host_ip)
            or _is_unusable_server_identity(host_str)
            or _is_unusable_server_identity(status_name)
        ):
            continue
        full_name = (
            db_server.name
            if db_server
            and db_server.name
            and (
                status_name in (None, "", status_identifier, key, host_str)
                or _name_key(db_server.name) == _name_key(status_name)
            )
            else status_name
        ) or status.get("_server") or "Unknown"
        if not _match_name(full_name):
            continue

        players_data = status.get("players") or []
        ip_info = ip_info_by_ip.get(str(host_ip or "").strip())
        entry = {
            "name": full_name,
            "short_name": parse_short_name(full_name) or (db_server.short_name if db_server else None) or status.get("short_name"),
            "full_name": full_name,
            "ip": host_ip,
            "port": host_port,
            "region": None,
            "map": None,
            "playlist": None,
            "player_count": len(players_data),
            "max_players": status.get("max_players", 0),
            "has_status": True,
            "ping": _first_positive_int(
                ip_info.ping if ip_info else None,
                db_server.ping if db_server else None,
                status.get("server_ping"),
            ),
            "country": status.get("country"),
            "host": host_str,
        }
        if is_admin:
            entry["admin_region"] = status.get("region")
        if not simple:
            player_list = []
            access_server_id = key or status.get("_server") or status.get("server_id")
            for p in players_data:
                p_info = await player_access_service.build_online_player_info(
                    p,
                    is_admin=is_admin,
                    server_id=access_server_id,
                )
                player_list.append(p_info)
            entry["players"] = player_list
        _strip_public_server_fields(entry)
        results.append(entry)

    return results


def _parse_host_port(value: str) -> tuple[str, int]:
    host_text = (value or "").strip()
    if ":" not in host_text:
        return host_text, 0

    host_part, _, port_part = host_text.rpartition(":")
    port = _safe_int(port_part)
    if not host_part or not port:
        return host_text, 0
    return host_part, port


async def set_server_alias(host: str, short_name: str | None) -> tuple[dict | None, str | None]:
    host_text, port = _parse_host_port(host)
    if not host_text:
        return None, "not_found"

    query = Server.filter(host=host_text)
    if port:
        query = query.filter(port=port)
    candidates = await query.order_by("-last_seen_at").limit(2).all()
    if not candidates:
        return None, "not_found"
    if not port and len(candidates) > 1:
        return {"host": host_text}, "ambiguous_host"
    server = candidates[0]

    normalized = (short_name or "").strip() or None
    if normalized:
        conflict = await Server.filter(short_name=normalized).exclude(id=server.id).first()
        if conflict:
            return {"host": conflict.host}, "alias_conflict"

    server.short_name = normalized  # type: ignore[assignment]
    await server.save(update_fields=["short_name", "updated_at"])
    return {"id": server.id, "host": server.host, "short_name": server.short_name}, None
