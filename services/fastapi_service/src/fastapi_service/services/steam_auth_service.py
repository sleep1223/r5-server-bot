"""Steam Web API client used by the pylon master-server endpoints.

Validates an `AuthSessionTicket` produced by `ISteamUser::GetAuthSessionTicket`
on the game client and returns the resolved Steam ID + persona.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from loguru import logger
from shared_lib.config import settings

STEAM_AUTH_URL = "https://api.steampowered.com/ISteamUserAuth/AuthenticateUserTicket/v1/"
STEAM_SUMMARY_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"


class SteamAuthError(Exception):
    """Raised when Steam ticket validation fails for any reason."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(slots=True)
class SteamAuthResult:
    steamid: str
    persona: str | None = None


async def authenticate_steam_ticket(
    ticket: str,
    *,
    app_id: str | None = None,
    api_key: str | None = None,
) -> SteamAuthResult:
    """Validate a Steam auth ticket and return the resolved Steam identity.

    Mirrors the behaviour of `r5v_master_server/src/lib/steam.ts`:
    - Hex ticket, uppercased.
    - One automatic retry on transient "Invalid ticket" responses.
    - Persona lookup via GetPlayerSummaries (best-effort, optional).
    """

    key = api_key or settings.steam_web_api_key
    app = app_id or settings.steam_app_id

    if not key:
        logger.error("steam_web_api_key is not configured")
        raise SteamAuthError("steam_api_key_missing", "Steam Web API key is not configured")
    if not app:
        raise SteamAuthError("steam_app_id_missing", "Steam app id is not configured")

    formatted_ticket = ticket.upper()

    async with httpx.AsyncClient(timeout=settings.steam_auth_timeout) as client:
        steamid = await _validate_ticket(client, key, app, formatted_ticket)

        persona: str | None = None
        if settings.steam_persona_lookup:
            persona = await _fetch_persona(client, key, steamid)

    return SteamAuthResult(steamid=steamid, persona=persona)


async def _validate_ticket(
    client: httpx.AsyncClient,
    key: str,
    app: str,
    ticket: str,
    *,
    attempt: int = 0,
) -> str:
    params = {"key": key, "appid": str(app), "ticket": ticket}

    try:
        resp = await client.get(STEAM_AUTH_URL, params=params)
    except httpx.HTTPError as exc:
        logger.error(f"Steam auth HTTP error: {exc}")
        raise SteamAuthError("steam_auth_network_error", str(exc)) from exc

    payload: dict = {}
    try:
        payload = resp.json()
    except ValueError:
        pass

    response = (payload or {}).get("response") or {}
    params_obj = response.get("params") or {}
    err_obj = response.get("error") or {}

    if resp.status_code == 200 and params_obj.get("steamid"):
        steamid = str(params_obj["steamid"])
        logger.debug(f"Steam ticket validated for {steamid}")
        return steamid

    err_desc = err_obj.get("errordesc") or f"steam_auth_failed_{resp.status_code}"
    logger.warning(f"Steam ticket rejected: {err_desc} (status={resp.status_code})")

    # Steam frequently returns "Invalid ticket" the first time when there is a
    # tiny race between the client generating the ticket and Steam's backend
    # registering it. Retry exactly once.
    if err_desc == "Invalid ticket" and attempt == 0:
        await asyncio.sleep(2.0)
        return await _validate_ticket(client, key, app, ticket, attempt=attempt + 1)

    raise SteamAuthError("steam_auth_failed", err_desc)


async def _fetch_persona(client: httpx.AsyncClient, key: str, steamid: str) -> str | None:
    try:
        resp = await client.get(
            STEAM_SUMMARY_URL,
            params={"key": key, "steamids": steamid},
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        logger.debug(f"Steam persona lookup failed (network): {exc}")
        return None

    if resp.status_code != 200:
        logger.debug(f"Steam persona lookup non-200: {resp.status_code}")
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    players = ((data or {}).get("response") or {}).get("players") or []
    if players:
        name = players[0].get("personaname")
        if isinstance(name, str) and name:
            return name
    return None
