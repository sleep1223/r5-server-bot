from fastapi_service.core.cache import server_cache
from fastapi_service.core.utils import parse_short_name


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_server_info() -> list[dict]:
    return list(server_cache.servers.values())


def list_servers(
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
    for s_status in server_cache.servers.values():
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

    raw = server_cache.raw_response
    raw_list: list[dict] = []
    if isinstance(raw, dict):
        raw_servers = raw.get("servers")
        if isinstance(raw_servers, list):
            raw_list = [s for s in raw_servers if isinstance(s, dict)]

    # cn_only 模式：只返回已同步的本地服务器；raw 列表未出现的也要能返回（避免拉取失败时丢失）
    results: list[dict] = []

    def _match_name(name: str) -> bool:
        if not server_name:
            return True
        return server_name.lower() in (name or "").lower()

    seen_keys: set[str] = set()

    for s in raw_list:
        ip = str(s.get("ip") or "")
        port = _safe_int(s.get("port"))
        key = f"{ip}:{port}" if ip and port else ""
        status = synced_by_key.get(key) if key else None
        has_status = status is not None
        if cn_only and not has_status:
            continue

        full_name = s.get("name") or (status.get("_api_name") if status else None) or (status.get("hostname") if status else None) or "Unknown"
        if not _match_name(full_name):
            continue

        short_name = (status.get("short_name") if status else None) or parse_short_name(full_name)
        raw_player_count = _safe_int(s.get("playerCount") or s.get("numPlayers"))
        raw_max_players = _safe_int(s.get("maxPlayers"))

        entry: dict = {
            "name": full_name,
            "short_name": short_name,
            "full_name": full_name,
            "ip": ip or None,
            "port": port or None,
            "region": s.get("region"),
            "map": s.get("map"),
            "playlist": s.get("playlist"),
            "player_count": raw_player_count,
            "max_players": raw_max_players,
            "has_status": has_status,
        }

        if status:
            players_data = status.get("players") or []
            entry["player_count"] = len(players_data)
            entry["max_players"] = status.get("max_players") or raw_max_players
            entry["ping"] = status.get("server_ping", 0)
            entry["country"] = status.get("country")
            entry["host"] = status.get("_server")
            if is_admin:
                entry["admin_region"] = status.get("region")

            if not simple:
                player_list = []
                for p in players_data:
                    p_info: dict = {
                        "name": p.get("name", "Unknown"),
                        "country": p.get("country"),
                    }
                    if is_admin:
                        p_info["region"] = p.get("region")
                    player_list.append(p_info)
                entry["players"] = player_list
        elif not simple:
            entry["players"] = []

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
            for p in players_data:
                p_info = {
                    "name": p.get("name", "Unknown"),
                    "country": p.get("country"),
                }
                if is_admin:
                    p_info["region"] = p.get("region")
                player_list.append(p_info)
            entry["players"] = player_list
        results.append(entry)

    return results
