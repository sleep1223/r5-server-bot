from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")


class ServerCache:
    """封装服务器状态缓存，提供类型安全的访问方法。"""

    def __init__(self) -> None:
        self._servers: dict[str, dict] = {}
        self._raw_response: dict[str, object] = {}
        self._ban_locations: dict[int, dict[str, object]] = {}

    # ── Server cache ──

    @property
    def servers(self) -> dict[str, dict]:
        return self._servers

    def update_servers(self, data: dict[str, dict]) -> None:
        self._servers.clear()
        self._servers.update(data)

    def set_server(self, key: str, data: dict) -> None:
        self._servers[key] = data

    def retain_servers(self, keys: set[str]) -> None:
        for k in list(self._servers.keys()):
            if k not in keys:
                self._servers.pop(k, None)

    # ── Raw server response ──

    @property
    def raw_response(self) -> dict[str, object]:
        return self._raw_response

    def update_raw_response(self, data: dict) -> None:
        self._raw_response.clear()
        self._raw_response.update(data)

    # ── Ban location cache ──

    @property
    def ban_locations(self) -> dict[int, dict[str, object]]:
        return self._ban_locations

    def cache_ban_location(self, nucleus_id: int, *, server_name: str, server_host: str, server_port: int) -> None:
        from .utils import parse_short_name

        short_name = parse_short_name(server_name)
        self._ban_locations[nucleus_id] = {
            "server_name": server_name,
            "short_name": short_name,
            "server_host": server_host,
            "server_port": server_port,
            "cached_at": datetime.now(CN_TZ).isoformat(),
        }

    def clear_ban_location(self, nucleus_id: int) -> None:
        self._ban_locations.pop(nucleus_id, None)

    # ── Player location lookup ──

    def get_online_location(self, nucleus_id: int) -> dict | None:
        p_id_str = str(nucleus_id)
        for s_status in self._servers.values():
            for p_data in s_status.get("players", []):
                if str(p_data.get("uniqueid")) == p_id_str:
                    host_port = s_status.get("_server", ":0").split(":")
                    host = host_port[0]
                    port = int(host_port[1]) if len(host_port) > 1 else 0
                    return {
                        "server_name": s_status.get("_api_name") or s_status.get("hostname"),
                        "server_host": host,
                        "server_port": port,
                        "online_at": p_data.get("online_at"),
                        "ping": p_data.get("ping", 0),
                        "short_name": s_status.get("short_name"),
                        "country": s_status.get("country"),
                        "region": s_status.get("region"),
                        "server_ping": s_status.get("server_ping", 0),
                    }
        return None

    def get_cached_ban_location(self, nucleus_id: int) -> dict | None:
        cached = self._ban_locations.get(nucleus_id)
        if not cached:
            return None

        server_host = str(cached.get("server_host") or "")
        try:
            server_port = int(str(cached.get("server_port", 0)))
        except Exception:
            server_port = 0

        server_name = str(cached.get("server_name") or f"{server_host}:{server_port}")
        return {
            "server_name": server_name,
            "server_host": server_host,
            "server_port": server_port,
            "online_at": None,
            "ping": 0,
            "short_name": cached.get("short_name"),
            "country": None,
            "region": None,
            "server_ping": 0,
            "_from_ban_cache": True,
        }

    def get_online_servers(self) -> list[dict]:
        servers = []
        for server_key, s_status in self._servers.items():
            try:
                host, port_str = str(server_key).rsplit(":", 1)
                port = int(port_str)
            except Exception:
                continue
            servers.append({
                "server_key": str(server_key),
                "server_name": s_status.get("_api_name") or s_status.get("hostname") or str(server_key),
                "server_host": host,
                "server_port": port,
            })
        return servers


server_cache = ServerCache()
