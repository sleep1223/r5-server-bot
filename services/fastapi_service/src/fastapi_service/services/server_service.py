from fastapi_service.core.cache import server_cache
from fastapi_service.core.utils import parse_short_name


def get_raw_server_list() -> dict:
    return dict(server_cache.raw_response)


def get_server_info() -> list[dict]:
    return list(server_cache.servers.values())


def get_server_status(*, server_name: str | None = None, is_admin: bool = False) -> list[dict]:
    results = []

    for server_data in server_cache.servers.values():
        full_name = server_data.get("_api_name") or server_data.get("hostname") or server_data.get("_server", "Unknown")

        if server_name and server_name.lower() not in full_name.lower():
            continue

        short_name = server_data.get("short_name")
        if not short_name:
            short_name = parse_short_name(full_name)

        player_count = len(server_data.get("players", []))

        host_str = server_data.get("_server", "")
        host_ip = server_data.get("ip")
        if not host_ip and host_str:
            host_ip = host_str.split(":")[0]

        host_port = server_data.get("port")
        if not host_port and host_str and ":" in host_str:
            try:
                host_port = int(host_str.split(":")[1])
            except Exception:
                host_port = 0

        player_list = []
        for p in server_data.get("players", []):
            p_info: dict = {"name": p.get("name", "Unknown"), "country": p.get("country")}
            if is_admin:
                p_info["region"] = p.get("region")
            player_list.append(p_info)

        results.append({
            "name": full_name,
            "short_name": short_name,
            "full_name": full_name,
            "player_count": player_count,
            "max_players": server_data.get("max_players", 0),
            "ping": server_data.get("server_ping", 0),
            "ip": host_ip,
            "port": host_port,
            "country": server_data.get("country"),
            "region": server_data.get("region"),
            "host": host_str,
            "players": player_list,
        })

    return results
