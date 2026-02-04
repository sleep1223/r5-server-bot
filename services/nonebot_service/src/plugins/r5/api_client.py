from typing import Any, Dict, Optional

import httpx
from nonebot import get_plugin_config

from .config import Config


class R5ApiClient:
    def __init__(self):
        self.config = get_plugin_config(Config)
        self.base_url = self.config.r5_api_base
        self.token = self.config.r5_api_token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, endpoint: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient() as client:
            return await client.request(
                method, f"{self.base_url}{endpoint}", headers=self.headers, **kwargs
            )

    async def get_kd_leaderboard(
        self,
        range_type: str = "all",
        limit: int = 20,
        sort: str = "kd",
        min_kills: int = 100,
        timeout: float = 3.0,
    ) -> httpx.Response:
        params = {
            "range": range_type,
            "limit": limit,
            "sort": sort,
            "min_kills": min_kills,
        }
        return await self._request(
            "GET", "/leaderboard/kd", params=params, timeout=timeout
        )

    async def get_player_vs_all(
        self, target: str, timeout: float = 3.0
    ) -> httpx.Response:
        return await self._request("GET", f"/players/{target}/vs_all", timeout=timeout)

    async def get_server_status(
        self, server_name: Optional[str] = None, timeout: float = 5.0
    ) -> httpx.Response:
        params = {}
        if server_name:
            params["server_name"] = server_name
        return await self._request(
            "GET", "/server/status", params=params, timeout=timeout
        )

    async def ban_player(self, target: str, timeout: float = 5.0) -> httpx.Response:
        return await self._request("POST", f"/players/{target}/ban", timeout=timeout)

    async def kick_player(self, target: str, timeout: float = 5.0) -> httpx.Response:
        return await self._request("POST", f"/players/{target}/kick", timeout=timeout)

    async def unban_player(self, target: str, timeout: float = 5.0) -> httpx.Response:
        return await self._request("POST", f"/players/{target}/unban", timeout=timeout)

    async def query_player(self, query: str, timeout: float = 5.0) -> httpx.Response:
        params = {"q": query}
        return await self._request(
            "GET", "/players/query", params=params, timeout=timeout
        )

    async def get_donations(
        self, limit: int = 1000, offset: int = 0, timeout: float = 5.0
    ) -> httpx.Response:
        params = {"limit": limit, "offset": offset}
        return await self._request(
            "GET", "/donations", params=params, timeout=timeout
        )

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
        return await self._request(
            "DELETE", f"/donations/{donation_id}", timeout=timeout
        )


# Global instance
api_client = R5ApiClient()
