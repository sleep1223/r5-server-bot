"""Pylon master-server endpoints consumed by the R5 SDK game client / server.

These routes implement the subset of the r5reloaded master-server protocol
that lets a Steam-authenticated client (and an r5r_sdk-based dedicated
server) talk to this bot as if it were a real master server.

Implemented endpoints:

- ``POST /client/auth``           — Steam ticket → short-lived RS256 JWT
- ``POST /client/authenticate``   — legacy r5r_sdk alias of /client/auth
- ``POST /server/auth/keyinfo``   — JWT public key distribution
- ``POST /banlist/isBanned``      — single-player ban check
- ``POST /banlist/bulkCheck``     — periodic bulk ban check
- ``POST /eula``                  — EULA shim (clients refuse to call other
                                     pylon endpoints until EULA is "accepted")
- ``POST /servers/add``           — dedicated server keep-alive shim

Response shapes intentionally match what the SDK's `CPylon` parser expects;
do **not** wrap them in the project's `success()` envelope.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from fastapi_service.services.jwt_auth_service import (
    JwtKeyError,
    create_auth_token,
    get_public_key_base64,
    get_public_key_hash,
    normalize_server_endpoint,
)
from fastapi_service.services.pylon_db_service import (
    BanLookupResult,
    lookup_player_ban_by_persona_id,
    write_steam_auth_log,
)
from fastapi_service.services.steam_auth_service import (
    SteamAuthError,
    authenticate_steam_ticket,
)

router = APIRouter(tags=["pylon"])

_STEAM_ID_RE = re.compile(r"^\d{17}$")


# ---------------------------------------------------------------------------
# /client/auth — Steam ticket → JWT
# ---------------------------------------------------------------------------


class ClientAuthRequest(BaseModel):
    """Body sent by the game client when authenticating for a connection.

    Field names match what r5v_sdk's `CPylon::AuthForConnection` serializes:
    `id` is the Steam ID 64 (as a string to avoid JSON number precision loss),
    `ip` is the target gameserver address, `code` is the legacy Origin auth
    code (always empty in Steam mode), `steamTicket` is the hex-encoded
    `GetAuthSessionTicket` blob, and `steamUsername` is the player's display
    name on the client side.

    The legacy r5r_sdk client serializes `id` as a JSON number; we accept
    both shapes via `model_config(coerce_numbers_to_str=True)` so the same
    handler can serve `/client/authenticate` as well.
    """

    model_config = ConfigDict(coerce_numbers_to_str=True)

    id: str
    ip: str
    code: str | None = ""
    steamTicket: str = Field(..., min_length=1)
    steamUsername: str | None = None
    reqIp: str | None = None  # optional, set by the deployer's reverse proxy


def _auth_error(http_status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"success": False, "error": message},
    )


def _client_ip(request: Request, body: ClientAuthRequest) -> str | None:
    if body.reqIp:
        return body.reqIp
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return None


async def _handle_client_auth(request: Request, body: ClientAuthRequest) -> JSONResponse:
    user_id = (body.id or "").strip()
    if not _STEAM_ID_RE.match(user_id):
        return _auth_error(400, "error: invalid Steam userId")

    if not body.ip:
        return _auth_error(400, "error: missing server ip")

    client_ip = _client_ip(request, body)
    server_endpoint = normalize_server_endpoint(body.ip)

    # ---- Steam Web API validation -----------------------------------------
    try:
        steam = await authenticate_steam_ticket(body.steamTicket)
    except SteamAuthError as exc:
        logger.warning(f"Steam ticket validation failed for {user_id}: {exc.code}")
        await write_steam_auth_log(
            steam_id=int(user_id),
            persona_name=body.steamUsername,
            server_endpoint=server_endpoint,
            client_ip=client_ip,
            success=False,
            error_code=exc.code,
        )
        return _auth_error(401, f"error: steam authentication failed ({exc.code})")
    except Exception:  # pragma: no cover - defensive
        logger.exception("Unexpected error during Steam ticket validation")
        await write_steam_auth_log(
            steam_id=int(user_id),
            persona_name=body.steamUsername,
            server_endpoint=server_endpoint,
            client_ip=client_ip,
            success=False,
            error_code="internal_error",
        )
        return _auth_error(500, "error: internal server error")

    if user_id != steam.steamid:
        logger.info(
            f"Steam ID mismatch — client claimed {user_id}, "
            f"validated {steam.steamid}; using validated id"
        )
        user_id = steam.steamid

    # ---- Ban gate ----------------------------------------------------------
    ban: BanLookupResult = await lookup_player_ban_by_persona_id(int(user_id))
    if ban.is_banned:
        logger.info(
            f"Steam-auth banned player {user_id} blocked: reason={ban.reason}"
        )
        await write_steam_auth_log(
            steam_id=int(user_id),
            persona_name=steam.persona or body.steamUsername,
            server_endpoint=server_endpoint,
            client_ip=client_ip,
            success=False,
            error_code=f"banned:{ban.reason or 'unknown'}",
        )
        return _auth_error(
            403,
            f"error: account banned ({ban.reason or 'unknown'})",
        )

    # ---- Sign + return ----------------------------------------------------
    display_name = steam.persona or body.steamUsername or "steam_user"
    try:
        token = create_auth_token(user_id, display_name, server_endpoint)
    except JwtKeyError as exc:
        logger.error(f"JWT signing failed: {exc}")
        await write_steam_auth_log(
            steam_id=int(user_id),
            persona_name=display_name,
            server_endpoint=server_endpoint,
            client_ip=client_ip,
            success=False,
            error_code="jwt_signing_unavailable",
        )
        return _auth_error(500, "error: token signing unavailable")

    await write_steam_auth_log(
        steam_id=int(user_id),
        persona_name=display_name,
        server_endpoint=server_endpoint,
        client_ip=client_ip,
        success=True,
        error_code=None,
    )

    return JSONResponse(
        status_code=200,
        content={"success": True, "token": token},
    )


@router.post("/client/auth")
async def client_auth(request: Request, body: ClientAuthRequest) -> JSONResponse:
    return await _handle_client_auth(request, body)


@router.post("/client/authenticate")
async def client_authenticate_legacy(
    request: Request, body: ClientAuthRequest
) -> JSONResponse:
    """Legacy r5r_sdk path. Same handler — `id` may be a JSON number here."""
    return await _handle_client_auth(request, body)


# ---------------------------------------------------------------------------
# /server/auth/keyinfo — public key distribution
# ---------------------------------------------------------------------------


class KeyInfoRequest(BaseModel):
    version: str | None = None
    keyHash: str | None = None


@router.post("/server/auth/keyinfo")
async def server_auth_keyinfo(body: KeyInfoRequest | None = None) -> JSONResponse:
    """Return the JWT verification public key.

    The game server caches the key, then re-polls every
    `pylon_auth_refresh_interval` seconds. If `keyHash` matches the current
    public key hash we respond with `requireUpdate: false` and skip the body,
    matching r5r_sdk's `CPylon::GetAuthKey` short-circuit at pylon.cpp:504.
    """
    try:
        current_hash = get_public_key_hash()
    except JwtKeyError as exc:
        logger.error(f"Cannot serve JWT keyinfo: {exc}")
        return JSONResponse(
            status_code=500,
            content={"requireUpdate": False, "error": "key unavailable"},
        )

    requested_hash = (body.keyHash if body else None) or ""
    if requested_hash and requested_hash == current_hash:
        return JSONResponse(status_code=200, content={"requireUpdate": False})

    return JSONResponse(
        status_code=200,
        content={
            "requireUpdate": True,
            "keyData": get_public_key_base64(),
            "keyHash": current_hash,
        },
    )


# ---------------------------------------------------------------------------
# /banlist/isBanned — server-side per-connect ban check
# ---------------------------------------------------------------------------


class BanCheckRequest(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    name: str | None = None
    id: str
    ip: str | None = None


def _ban_response(ban: BanLookupResult) -> dict[str, Any]:
    if not ban.is_banned:
        return {"banned": False}
    return {
        "banned": True,
        "reason": ban.reason or "#DISCONNECT_BANNED",
        "banType": ban.ban_type,
    }


@router.post("/banlist/isBanned")
async def banlist_is_banned(body: BanCheckRequest) -> JSONResponse:
    raw_id = (body.id or "").strip()
    if not raw_id:
        return JSONResponse(status_code=200, content={"banned": False})

    try:
        persona_id = int(raw_id)
    except ValueError:
        return JSONResponse(status_code=200, content={"banned": False})

    ban = await lookup_player_ban_by_persona_id(persona_id)
    return JSONResponse(status_code=200, content=_ban_response(ban))


# ---------------------------------------------------------------------------
# /banlist/bulkCheck — periodic bulk check from r5r_sdk dedi
# ---------------------------------------------------------------------------


class BulkCheckPlayer(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    id: str
    ip: str | None = None


class BulkCheckRequest(BaseModel):
    players: list[BulkCheckPlayer] = Field(default_factory=list)


@router.post("/banlist/bulkCheck")
async def banlist_bulk_check(body: BulkCheckRequest) -> JSONResponse:
    banned_players: list[dict[str, Any]] = []
    for player in body.players:
        try:
            persona_id = int(player.id)
        except ValueError:
            continue

        ban = await lookup_player_ban_by_persona_id(persona_id)
        if ban.is_banned:
            banned_players.append(
                {
                    "id": player.id,
                    "ip": player.ip or "",
                    "reason": ban.reason or "#DISCONNECT_BANNED",
                    "banType": ban.ban_type,
                }
            )

    return JSONResponse(
        status_code=200,
        content={"bannedPlayers": banned_players},
    )


# ---------------------------------------------------------------------------
# /eula — EULA shim
# ---------------------------------------------------------------------------


_EULA_VERSION = 1
_EULA_LANGUAGE = "english"
_EULA_CONTENTS = (
    "By connecting to this server cluster you agree to abide by the server "
    "rules. This master server is operated by the r5-server-bot project and "
    "is not affiliated with Electronic Arts or Respawn Entertainment."
)


@router.post("/eula")
async def eula() -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "data": {
                "version": _EULA_VERSION,
                "language": _EULA_LANGUAGE,
                "contents": _EULA_CONTENTS,
            }
        },
    )


# ---------------------------------------------------------------------------
# /servers/add — dedicated server keep-alive shim
# ---------------------------------------------------------------------------


class ServersAddRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    description: str | None = None
    hidden: bool = False
    map: str | None = None
    playlist: str | None = None
    ip: str | None = None
    port: int | None = None
    key: str | None = None
    checksum: int | None = None
    version: str | None = None
    numPlayers: int | None = None
    maxPlayers: int | None = None
    timeStamp: int | None = None
    authEnabled: bool | None = None


@router.post("/servers/add")
async def servers_add(body: ServersAddRequest) -> JSONResponse:
    """Echo back what the dedi sent so its keep-alive logging stays clean.

    The bot already discovers servers via its own `fetch_servers` task, so we
    intentionally do not persist what dedis self-report here. Hidden servers
    receive a deterministic dummy token derived from their checksum so the
    `byToken` lookup roundtrip continues to work for the same dedi process.
    """
    payload: dict[str, Any] = {
        "ip": body.ip or "",
        "port": body.port or 0,
    }
    if body.hidden:
        # Stable dummy token: server can re-resolve itself via /server/byToken
        # if needed, though we do not implement that endpoint here.
        payload["token"] = f"selfhost:{body.checksum or 0}:{body.port or 0}"
    return JSONResponse(status_code=200, content=payload)


__all__ = ["router"]
