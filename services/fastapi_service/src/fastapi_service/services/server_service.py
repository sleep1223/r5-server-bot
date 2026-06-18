from shared_lib.models import Server
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


def get_server_info() -> list[dict]:
    return list(server_cache.servers.values())


async def list_servers(
    *,
    server_name: str | None = None,
    simple: bool = False,
    cn_only: bool = False,
    is_admin: bool = False,
) -> list[dict]:
    """统一的服务器列表查询接口。

    - 默认返回远程服务器列表（raw）与本地 RCON 同步状态合并后的结果。
    - ``simple=True`` 返回精简字段，去除在线玩家列表等重字段。
    - ``cn_only=True`` 只返回已经通过 RCON 同步到本地的服务器
      （即 r5_target_keys 配置命中的中国服），附带玩家详情与延迟。
    - ``server_name`` 对服务器名做不区分大小写的模糊过滤。
    """

    synced_by_key: dict[str, dict] = {}
    synced_by_ip: dict[str, dict] = {}
    synced_by_server_id: dict[str, dict] = {}
    for s_status in server_cache.servers.values():
        server_identifier = str(s_status.get("server_id") or "").strip()
        if server_identifier:
            synced_by_server_id[server_identifier] = s_status

        host = str(s_status.get("ip") or "")
        port = s_status.get("port")
        if not host or not port:
            server_str = str(s_status.get("_server") or "")
            if ":" in server_str:
                host_part, _, port_part = server_str.rpartition(":")
                if host_part and port_part:
                    host = host or host_part
                    port = port or _safe_int(port_part)
        if host and port:
            synced_by_key[f"{host}:{port}"] = s_status
            synced_by_ip.setdefault(host, s_status)

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
    db_by_host = {s.host: s for s in online_server_rows if s.host}

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
        if db_server is None and ip:
            db_server = db_by_host.get(ip)
        if db_server:
            if not ip:
                ip = db_server.host
            if not port:
                port = db_server.port

        key = f"{ip}:{port}" if ip and port else ""
        status = None
        for identifier in server_identifiers:
            status = synced_by_server_id.get(identifier)
            if status:
                break
        if status is None:
            status = synced_by_key.get(key) if key else None
        if not ip and db_server is None and status is None:
            continue
        # raw 缺 port 时按 ip 兜底匹配，避免同一服务器在结果里出现两次
        if status is None and ip and not port:
            status = synced_by_ip.get(ip)
            if status:
                port = _safe_int(status.get("port")) or port
                key = f"{ip}:{port}" if port else ip
        full_name = s.get("name") or (status.get("_api_name") if status else None) or (status.get("hostname") if status else None) or "Unknown"
        is_cn_raw = _is_cn_raw_server(s, full_name)
        has_status = status is not None or db_server is not None or bool(ip)
        if cn_only and not (status is not None or is_cn_raw):
            continue
        if not _match_name(full_name):
            continue

        short_name = (status.get("short_name") if status else None) or parse_short_name(full_name)
        raw_player_count = _safe_int(s.get("playerCount") or s.get("numPlayers"))
        raw_max_players = _safe_int(s.get("maxPlayers"))
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
        }

        if status:
            players_data = status.get("players") or []
            # 只有当 RCON 真的解析到玩家表（即使表里 0 人）时才用 RCON 的数；
            # 解析失败/超时（players_parsed=False）回退到 raw playerCount，避免误报 0
            players_parsed = bool(status.get("players_parsed"))
            entry["player_count"] = len(players_data) if players_parsed else raw_player_count
            entry["max_players"] = status.get("max_players") or raw_max_players
            entry["ping"] = status.get("server_ping", 0)
            entry["country"] = status.get("country")
            entry["host"] = status.get("_server")
            if is_admin:
                entry["admin_region"] = status.get("region")

            if not simple:
                player_list = []
                access_server_id = status.get("server_id") or status.get("_server") or key
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
        results.append(entry)

    # 补充 raw 列表中缺失但已 RCON 同步到的服务器（例如 raw 拉取失败或服务器离开 master list）
    for key, status in synced_by_key.items():
        if key in seen_keys:
            continue
        full_name = status.get("_api_name") or status.get("hostname") or status.get("_server") or "Unknown"
        if not _match_name(full_name):
            continue

        host_str = status.get("_server") or key
        host_ip = status.get("ip")
        host_port = status.get("port")
        if not host_ip and ":" in host_str:
            host_ip = host_str.split(":", 1)[0]
        if not host_port and ":" in host_str:
            host_port = _safe_int(host_str.rsplit(":", 1)[-1])

        players_data = status.get("players") or []
        entry = {
            "name": full_name,
            "short_name": status.get("short_name") or parse_short_name(full_name),
            "full_name": full_name,
            "ip": host_ip,
            "port": host_port,
            "region": None,
            "map": None,
            "playlist": None,
            "player_count": len(players_data),
            "max_players": status.get("max_players", 0),
            "has_status": True,
            "ping": status.get("server_ping", 0),
            "country": status.get("country"),
            "host": host_str,
        }
        if is_admin:
            entry["admin_region"] = status.get("region")
        if not simple:
            player_list = []
            access_server_id = status.get("server_id") or status.get("_server") or key
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


async def set_server_alias(host: str, short_name: str | None) -> tuple[dict | None, str | None]:
    server = await Server.get_or_none(host=host)
    if not server:
        return None, "not_found"

    normalized = (short_name or "").strip() or None
    if normalized:
        conflict = await Server.filter(short_name=normalized).exclude(id=server.id).first()
        if conflict:
            return {"host": conflict.host}, "alias_conflict"

    server.short_name = normalized  # type: ignore[assignment]
    await server.save(update_fields=["short_name", "updated_at"])
    return {"id": server.id, "host": server.host, "short_name": server.short_name}, None
