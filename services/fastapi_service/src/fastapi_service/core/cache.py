from __future__ import annotations

import ipaddress
from datetime import datetime
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")
ACCESS_REPORT_TTL_SECONDS = 120


def _safe_int(value: object | None, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _normalize_ip(ip: object | None) -> str:
    text = str(ip or "").strip()
    if not text:
        return ""
    try:
        address = ipaddress.ip_address(text)
        return str(address.ipv4_mapped or address) if isinstance(address, ipaddress.IPv6Address) else str(address)
    except ValueError:
        return text


def _normalize_server_host(ip: object | None) -> str:
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
        from .utils import parse_short_name

        server_key = str(server_id or "").strip()
        if not server_key:
            return

        updated_at = datetime.now(CN_TZ)
        previous_report = self._access_reports.get(server_key) or {}
        previous_updated_at = previous_report.get("updated_at")
        previous_players = previous_report.get("players") or []
        reuse_previous_online_at = isinstance(previous_updated_at, datetime) and (updated_at - previous_updated_at).total_seconds() <= ACCESS_REPORT_TTL_SECONDS
        previous_online_at_by_uid = {}
        if reuse_previous_online_at and isinstance(previous_players, list):
            previous_online_at_by_uid = {
                str(p_data.get("uniqueid")): p_data.get("online_at")
                for p_data in previous_players
                if isinstance(p_data, dict) and p_data.get("uniqueid") is not None and isinstance(p_data.get("online_at"), datetime)
            }

        players = []
        for player in data.get("players") or []:
            uid = player.get("uid") or player.get("nucleusId") or player.get("uniqueid")
            if uid is None:
                continue
            uid_text = str(uid)
            players.append({
                "uniqueid": uid_text,
                "name": player.get("playerName") or player.get("name") or str(uid),
                "ip": _normalize_ip(player.get("ip")) or player.get("ip"),
                "port": player.get("port"),
                "country": player.get("country"),
                "region": player.get("region"),
                "input_device": player.get("inputDevice") or player.get("input_device") or player.get("input") or player.get("device"),
                "user_id": player.get("userId"),
                "handle": player.get("handle"),
                "signon_state": player.get("signonState"),
                "ping": _safe_int(player.get("ping"), 0),
                "loss": _safe_int(player.get("loss"), 0),
                "online_at": previous_online_at_by_uid.get(uid_text) or updated_at,
            })

        server_host = _normalize_server_host(data.get("serverIp")) or None
        server_port = _safe_int(data.get("serverPort"), 0)
        server_name = str(data.get("serverName") or data.get("hostname") or server_key)
        self._access_reports[server_key] = {
            "server_id": server_key,
            "server_name": server_name,
            "short_name": parse_short_name(server_name),
            "server_host": server_host,
            "server_port": server_port,
            "map": data.get("map"),
            "tick": data.get("tick"),
            "num_players": _safe_int(data.get("numPlayers"), len(players)),
            "max_players": _safe_int(data.get("maxPlayers"), 0),
            "players": players,
            "updated_at": updated_at,
        }

    def _fresh_access_reports(self, *, ttl_seconds: int = ACCESS_REPORT_TTL_SECONDS) -> dict[str, dict[str, object]]:
        now = datetime.now(CN_TZ)
        fresh: dict[str, dict[str, object]] = {}
        for server_key, report in list(self._access_reports.items()):
            updated_at = report.get("updated_at")
            if not isinstance(updated_at, datetime):
                self._access_reports.pop(server_key, None)
                continue
            if ttl_seconds > 0 and (now - updated_at).total_seconds() > ttl_seconds:
                self._access_reports.pop(server_key, None)
                continue
            fresh[server_key] = report
        return fresh

    def get_online_server_statuses(self, *, ttl_seconds: int = ACCESS_REPORT_TTL_SECONDS) -> list[dict]:
        statuses = []
        for server_key, report in self._fresh_access_reports(ttl_seconds=ttl_seconds).items():
            server_host = str(report.get("server_host") or "").strip()
            server_port = _safe_int(report.get("server_port"), 0)
            if (not server_host or not server_port) and ":" in server_key:
                host_part, _, port_part = server_key.rpartition(":")
                parsed_port = _safe_int(port_part)
                if host_part and parsed_port:
                    server_host = server_host or host_part
                    server_port = server_port or parsed_port

            players = report.get("players") or []
            if not isinstance(players, list):
                players = []

            server_name = str(report.get("server_name") or server_key)
            status = {
                "server_id": None,
                "_server": f"{server_host}:{server_port}" if server_host and server_port else server_key,
                "_api_name": server_name,
                "hostname": server_name,
                "short_name": report.get("short_name") or server_name,
                "ip": server_host or None,
                "port": server_port or None,
                "map": report.get("map"),
                "tick": report.get("tick"),
                "players": players,
                "players_parsed": True,
                "player_count": len(players),
                "num_players": report.get("num_players"),
                "max_players": report.get("max_players"),
                "server_ping": 0,
                "country": None,
                "region": None,
                "updated_at": report.get("updated_at"),
                "_from_access_report": True,
            }
            statuses.append(status)
        return statuses

    def get_online_nucleus_ids(self, *, ttl_seconds: int = ACCESS_REPORT_TTL_SECONDS) -> set[str]:
        online_ids: set[str] = set()
        for report in self._fresh_access_reports(ttl_seconds=ttl_seconds).values():
            players = report.get("players") or []
            if not isinstance(players, list):
                continue
            for p_data in players:
                if not isinstance(p_data, dict):
                    continue
                uid = str(p_data.get("uniqueid") or "").strip()
                if uid:
                    online_ids.add(uid)
        return online_ids

    def get_access_report_location(self, nucleus_id: int, *, ttl_seconds: int = 120) -> dict | None:
        p_id_str = str(nucleus_id)
        for server_id, report in self._fresh_access_reports(ttl_seconds=ttl_seconds).items():
            updated_at = report.get("updated_at")
            players = report.get("players") or []
            if not isinstance(players, list):
                continue
            for p_data in players:
                if not isinstance(p_data, dict):
                    continue
                if str(p_data.get("uniqueid")) != p_id_str:
                    continue
                server_host = report.get("server_host") or server_id
                server_port = _safe_int(report.get("server_port"), 0)
                online_at = p_data.get("online_at")
                if not isinstance(online_at, datetime):
                    online_at = updated_at
                return {
                    "server_id": server_id,
                    "server_name": report.get("server_name") or server_id,
                    "server_host": server_host,
                    "server_port": server_port,
                    "online_at": online_at,
                    "ping": p_data.get("ping", 0),
                    "loss": p_data.get("loss", 0),
                    "player_ip": p_data.get("ip"),
                    "player_country": p_data.get("country"),
                    "player_region": p_data.get("region"),
                    "input_device": p_data.get("input_device"),
                    "short_name": report.get("short_name") or report.get("server_name") or server_id,
                    "country": None,
                    "region": None,
                    "server_ping": 0,
                    "_from_access_report": True,
                }
        return None

    # ── Player location lookup ──

    def get_online_location(self, nucleus_id: int) -> dict | None:
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
            "loss": 0,
            "short_name": cached.get("short_name"),
            "country": None,
            "region": None,
            "server_ping": 0,
            "_from_ban_cache": True,
        }

    def get_online_servers(self) -> list[dict]:
        servers = []
        for s_status in self.get_online_server_statuses():
            host = str(s_status.get("ip") or "").strip()
            port = _safe_int(s_status.get("port"), 0)
            if not host or not port:
                continue
            servers.append({
                "server_key": str(s_status.get("_server") or f"{host}:{port}"),
                "server_name": s_status.get("_api_name") or s_status.get("hostname") or f"{host}:{port}",
                "server_host": host,
                "server_port": port,
            })
        return servers


server_cache = ServerCache()
