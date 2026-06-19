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
        self._access_reports: dict[str, dict[str, object]] = {}

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

    # ── SDK access online report cache ──

    def update_access_report(self, server_id: str, data: dict) -> None:
        server_key = str(server_id or "").strip()
        if not server_key:
            return

        players = []
        for player in data.get("players") or []:
            uid = player.get("uid") or player.get("nucleusId") or player.get("uniqueid")
            if uid is None:
                continue
            players.append({
                "uniqueid": str(uid),
                "name": player.get("playerName") or player.get("name") or str(uid),
                "ip": player.get("ip"),
                "port": player.get("port"),
                "country": player.get("country"),
                "region": player.get("region"),
                "input_device": player.get("inputDevice") or player.get("input_device") or player.get("input") or player.get("device"),
                "user_id": player.get("userId"),
                "handle": player.get("handle"),
                "signon_state": player.get("signonState"),
            })

        self._access_reports[server_key] = {
            "server_id": server_key,
            "server_host": data.get("serverIp"),
            "server_port": data.get("serverPort"),
            "map": data.get("map"),
            "tick": data.get("tick"),
            "num_players": data.get("numPlayers"),
            "max_players": data.get("maxPlayers"),
            "players": players,
            "updated_at": datetime.now(CN_TZ),
        }

    def get_access_report_location(self, nucleus_id: int, *, ttl_seconds: int = 120) -> dict | None:
        p_id_str = str(nucleus_id)
        now = datetime.now(CN_TZ)
        for server_id, report in self._access_reports.items():
            updated_at = report.get("updated_at")
            if isinstance(updated_at, datetime) and (now - updated_at).total_seconds() > ttl_seconds:
                continue

            players = report.get("players") or []
            if not isinstance(players, list):
                continue
            for p_data in players:
                if not isinstance(p_data, dict):
                    continue
                if str(p_data.get("uniqueid")) != p_id_str:
                    continue
                server_host = report.get("server_host") or server_id
                server_port = report.get("server_port") or 0
                return {
                    "server_id": server_id,
                    "server_name": server_id,
                    "server_host": server_host,
                    "server_port": server_port,
                    "online_at": updated_at,
                    "ping": 0,
                    "player_ip": p_data.get("ip"),
                    "player_country": p_data.get("country"),
                    "player_region": p_data.get("region"),
                    "input_device": p_data.get("input_device"),
                    "short_name": server_id,
                    "country": None,
                    "region": None,
                    "server_ping": 0,
                    "_from_access_report": True,
                }
        return None

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
                        "player_ip": p_data.get("ip"),
                        "player_country": p_data.get("country"),
                        "player_region": p_data.get("region"),
                        "short_name": s_status.get("short_name"),
                        "country": s_status.get("country"),
                        "region": s_status.get("region"),
                        "server_ping": s_status.get("server_ping", 0),
                    }
        return self.get_access_report_location(nucleus_id)

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
