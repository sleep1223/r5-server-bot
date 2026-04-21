from typing import Any

import httpx
from nonebot import get_plugin_config

from .config import Config


class R5ApiClient:
    def __init__(self) -> None:
        self.config = get_plugin_config(Config)
        self.base_url = self.config.r5_api_base
        self.token = self.config.r5_api_token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, endpoint: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient() as client:
            return await client.request(method, f"{self.base_url}{endpoint}", headers=self.headers, **kwargs)

    async def get_kd_leaderboard(
        self,
        range_type: str = "all",
        page_no: int = 1,
        page_size: int = 20,
        sort: str = "kd",
        min_kills: int = 100,
        min_deaths: int = 0,
        server: str | None = None,
        timeout: float = 3.0,
    ) -> httpx.Response:
        params: dict[str, Any] = {
            "range": range_type,
            "page_no": page_no,
            "page_size": page_size,
            "sort": sort,
            "min_kills": min_kills,
            "min_deaths": min_deaths,
        }
        if server:
            params["server"] = server
        return await self._request("GET", "/leaderboard/kd", params=params, timeout=timeout)

    async def get_player_vs_all(
        self,
        target: str,
        page_no: int = 1,
        page_size: int = 20,
        sort: str = "kd",
        server: str | None = None,
        timeout: float = 3.0,
    ) -> httpx.Response:
        params: dict[str, Any] = {"page_no": page_no, "page_size": page_size, "sort": sort}
        if server:
            params["server"] = server
        return await self._request("GET", f"/players/{target}/vs_all", params=params, timeout=timeout)

    async def get_player_weapons(
        self,
        target: str,
        page_no: int = 1,
        page_size: int = 20,
        sort: str = "kd",
        server: str | None = None,
        timeout: float = 3.0,
    ) -> httpx.Response:
        params: dict[str, Any] = {"page_no": page_no, "page_size": page_size, "sort": sort}
        if server:
            params["server"] = server
        return await self._request("GET", f"/players/{target}/weapons", params=params, timeout=timeout)

    async def get_weapon_leaderboard(
        self,
        weapon: list[str] | None = None,
        range_type: str = "today",
        page_no: int = 1,
        page_size: int = 20,
        sort: str = "kd",
        min_kills: int = 1,
        min_deaths: int = 0,
        server: str | None = None,
        timeout: float = 3.0,
    ) -> httpx.Response:
        params: dict[str, Any] = {
            "range": range_type,
            "page_no": page_no,
            "page_size": page_size,
            "sort": sort,
            "min_kills": min_kills,
            "min_deaths": min_deaths,
        }
        if weapon:
            params["weapon"] = weapon
        if server:
            params["server"] = server
        return await self._request("GET", "/leaderboard/weapon", params=params, timeout=timeout)

    async def set_server_alias(self, host: str, short_name: str | None, timeout: float = 5.0) -> httpx.Response:
        data = {"short_name": short_name}
        return await self._request("PATCH", f"/server/by-host/{host}/alias", json=data, timeout=timeout)

    async def get_servers(
        self,
        server_name: str | None = None,
        *,
        simple: bool = False,
        cn_only: bool = False,
        timeout: float = 5.0,
    ) -> httpx.Response:
        params: dict[str, Any] = {}
        if server_name:
            params["server_name"] = server_name
        if simple:
            params["simple"] = "true"
        if cn_only:
            params["cn_only"] = "true"
        return await self._request("GET", "/server", params=params, timeout=timeout)

    async def ban_player(self, target: str, reason: str, timeout: float = 5.0) -> httpx.Response:
        params = {"reason": reason}
        return await self._request("POST", f"/players/{target}/ban", params=params, timeout=timeout)

    async def kick_player(self, target: str, reason: str, timeout: float = 5.0) -> httpx.Response:
        params = {"reason": reason}
        return await self._request("POST", f"/players/{target}/kick", params=params, timeout=timeout)

    async def unban_player(self, target: str, timeout: float = 12.0) -> httpx.Response:
        return await self._request("POST", f"/players/{target}/unban", timeout=timeout)

    async def query_player(self, query: str, page_no: int = 1, page_size: int = 20, timeout: float = 5.0) -> httpx.Response:
        params = {"q": query, "page_no": page_no, "page_size": page_size}
        return await self._request("GET", "/players/query", params=params, timeout=timeout)

    async def get_donations(self, page_no: int = 1, page_size: int = 1000, timeout: float = 5.0) -> httpx.Response:
        params = {"page_no": page_no, "page_size": page_size}
        return await self._request("GET", "/donations", params=params, timeout=timeout)

    async def create_donation(
        self,
        donor_name: str | None,
        amount: float,
        message: str | None = None,
        currency: str = "CNY",
        timeout: float = 5.0,
    ) -> httpx.Response:
        data = {
            "donor_name": donor_name,
            "amount": amount,
            "currency": currency,
            "message": message,
        }
        return await self._request("POST", "/donations", json=data, timeout=timeout)

    async def delete_donation(self, donation_id: int, timeout: float = 5.0) -> httpx.Response:
        return await self._request("DELETE", f"/donations/{donation_id}", timeout=timeout)

    # ── 绑定相关 ──────────────────────────────────────────────

    async def bind_player(self, platform: str, platform_uid: str, player_query: str, timeout: float = 5.0) -> httpx.Response:
        data = {"platform": platform, "platform_uid": platform_uid, "player_query": player_query}
        return await self._request("POST", "/user/bind", json=data, timeout=timeout)

    async def admin_bind_player(self, platform: str, platform_uid: str, player_query: str, timeout: float = 5.0) -> httpx.Response:
        data = {"platform": platform, "platform_uid": platform_uid, "player_query": player_query}
        return await self._request("POST", "/user/admin/bind", json=data, timeout=timeout)

    async def unbind_player(self, platform: str, platform_uid: str, timeout: float = 5.0) -> httpx.Response:
        params = {"platform": platform, "platform_uid": platform_uid}
        return await self._request("DELETE", "/user/bind", params=params, timeout=timeout)

    async def get_binding(self, platform: str, platform_uid: str, timeout: float = 5.0) -> httpx.Response:
        params = {"platform": platform, "platform_uid": platform_uid}
        return await self._request("GET", "/user/bind", params=params, timeout=timeout)

    # ── 组队相关 ──────────────────────────────────────────────

    async def create_team(self, platform: str, platform_uid: str, slots_needed: int, timeout: float = 5.0) -> httpx.Response:
        data = {"platform": platform, "platform_uid": platform_uid, "slots_needed": slots_needed}
        return await self._request("POST", "/teams", json=data, timeout=timeout)

    async def list_teams(self, page_no: int = 1, page_size: int = 20, timeout: float = 5.0) -> httpx.Response:
        params = {"page_no": page_no, "page_size": page_size}
        return await self._request("GET", "/teams", params=params, timeout=timeout)

    async def get_team(self, team_id: int, timeout: float = 5.0) -> httpx.Response:
        return await self._request("GET", f"/teams/{team_id}", timeout=timeout)

    async def join_team(self, team_id: int, platform: str, platform_uid: str, timeout: float = 5.0) -> httpx.Response:
        data = {"platform": platform, "platform_uid": platform_uid}
        return await self._request("POST", f"/teams/{team_id}/join", json=data, timeout=timeout)

    async def cancel_team(self, team_id: int, platform: str, platform_uid: str, timeout: float = 5.0) -> httpx.Response:
        data = {"platform": platform, "platform_uid": platform_uid}
        return await self._request("POST", f"/teams/{team_id}/cancel", json=data, timeout=timeout)

    async def leave_team(self, team_id: int, platform: str, platform_uid: str, timeout: float = 5.0) -> httpx.Response:
        data = {"platform": platform, "platform_uid": platform_uid}
        return await self._request("POST", f"/teams/{team_id}/leave", json=data, timeout=timeout)

    async def invite_player(self, team_id: int, platform: str, platform_uid: str, target_player_name: str, timeout: float = 5.0) -> httpx.Response:
        data = {"platform": platform, "platform_uid": platform_uid, "target_player_name": target_player_name}
        return await self._request("POST", f"/teams/{team_id}/invite", json=data, timeout=timeout)

    async def accept_invite(self, team_id: int, platform: str, platform_uid: str, timeout: float = 5.0) -> httpx.Response:
        data = {"platform": platform, "platform_uid": platform_uid}
        return await self._request("POST", f"/teams/{team_id}/accept", json=data, timeout=timeout)


# Global instance
api_client = R5ApiClient()
