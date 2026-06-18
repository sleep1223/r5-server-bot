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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from fastapi_service.services import pylon_service

router = APIRouter(tags=["pylon"])


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


def _json(response: pylon_service.PylonResponse) -> JSONResponse:
    return JSONResponse(status_code=response.status_code, content=response.content)


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
    response = await pylon_service.authenticate_client(
        user_id=body.id or "",
        server_ip=body.ip,
        steam_ticket=body.steamTicket,
        client_ip=_client_ip(request, body),
        steam_username=body.steamUsername,
    )
    return _json(response)


@router.post("/client/auth")
async def client_auth(request: Request, body: ClientAuthRequest) -> JSONResponse:
    return await _handle_client_auth(request, body)


@router.post("/client/authenticate")
async def client_authenticate_legacy(request: Request, body: ClientAuthRequest) -> JSONResponse:
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
    requested_hash = (body.keyHash if body else None) or ""
    return _json(pylon_service.get_keyinfo(requested_hash))


# ---------------------------------------------------------------------------
# /banlist/isBanned — server-side per-connect ban check
# ---------------------------------------------------------------------------


class BanCheckRequest(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    name: str | None = None
    id: str
    ip: str | None = None


@router.post("/banlist/isBanned")
async def banlist_is_banned(body: BanCheckRequest) -> JSONResponse:
    return _json(await pylon_service.check_player_ban(body.id or ""))


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
    players = [(player.id, player.ip) for player in body.players]
    return _json(await pylon_service.bulk_check_bans(players))


# ---------------------------------------------------------------------------
# /eula — EULA shim
# ---------------------------------------------------------------------------


@router.post("/eula")
async def eula() -> JSONResponse:
    return _json(pylon_service.get_eula())


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
    return _json(
        pylon_service.add_server(
            ip=body.ip,
            port=body.port,
            hidden=body.hidden,
            checksum=body.checksum,
        )
    )


__all__ = ["router"]
