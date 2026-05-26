from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

from fastapi_service.services.jwt_auth_service import (
    JwtKeyError,
    create_auth_token,
    get_public_key_base64,
    get_public_key_hash,
    normalize_server_endpoint,
)
from fastapi_service.services.pylon_db_service import (
    BanLookupResult,
    lookup_bans_by_persona_ids,
    lookup_player_ban_by_persona_id,
    write_steam_auth_log,
)
from fastapi_service.services.steam_auth_service import (
    SteamAuthError,
    authenticate_steam_ticket,
)

_STEAM_ID_RE = re.compile(r"^\d{17}$")
_EULA_VERSION = 1
_EULA_LANGUAGE = "english"
_EULA_CONTENTS = (
    "By connecting to this server cluster you agree to abide by the server "
    "rules. This master server is operated by the r5-server-bot project and "
    "is not affiliated with Electronic Arts or Respawn Entertainment."
)


@dataclass(frozen=True)
class PylonResponse:
    status_code: int
    content: dict[str, Any]


def _auth_error(http_status: int, message: str) -> PylonResponse:
    return PylonResponse(
        status_code=http_status,
        content={"success": False, "error": message},
    )


async def authenticate_client(
    *,
    user_id: str,
    server_ip: str,
    steam_ticket: str,
    client_ip: str | None,
    steam_username: str | None,
) -> PylonResponse:
    user_id = user_id.strip()
    if not _STEAM_ID_RE.match(user_id):
        return _auth_error(400, "error: invalid Steam userId")

    if not server_ip:
        return _auth_error(400, "error: missing server ip")

    server_endpoint = normalize_server_endpoint(server_ip)

    try:
        steam = await authenticate_steam_ticket(steam_ticket)
    except SteamAuthError as exc:
        logger.warning(f"Steam ticket validation failed for {user_id}: {exc.code}")
        await write_steam_auth_log(
            steam_id=int(user_id),
            persona_name=steam_username,
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
            persona_name=steam_username,
            server_endpoint=server_endpoint,
            client_ip=client_ip,
            success=False,
            error_code="internal_error",
        )
        return _auth_error(500, "error: internal server error")

    if user_id != steam.steamid:
        logger.info(
            f"Steam ID mismatch - client claimed {user_id}, "
            f"validated {steam.steamid}; using validated id"
        )
        user_id = steam.steamid

    ban = await lookup_player_ban_by_persona_id(int(user_id))
    if ban.is_banned:
        logger.info(f"Steam-auth banned player {user_id} blocked: reason={ban.reason}")
        await write_steam_auth_log(
            steam_id=int(user_id),
            persona_name=steam.persona or steam_username,
            server_endpoint=server_endpoint,
            client_ip=client_ip,
            success=False,
            error_code=f"banned:{ban.reason or 'unknown'}",
        )
        return _auth_error(
            403,
            f"error: account banned ({ban.reason or 'unknown'})",
        )

    display_name = steam.persona or steam_username or "steam_user"
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

    return PylonResponse(status_code=200, content={"success": True, "token": token})


def get_keyinfo(requested_hash: str | None) -> PylonResponse:
    try:
        current_hash = get_public_key_hash()
    except JwtKeyError as exc:
        logger.error(f"Cannot serve JWT keyinfo: {exc}")
        return PylonResponse(
            status_code=500,
            content={"requireUpdate": False, "error": "key unavailable"},
        )

    if requested_hash and requested_hash == current_hash:
        return PylonResponse(status_code=200, content={"requireUpdate": False})

    return PylonResponse(
        status_code=200,
        content={
            "requireUpdate": True,
            "keyData": get_public_key_base64(),
            "keyHash": current_hash,
        },
    )


def _ban_response(ban: BanLookupResult) -> dict[str, Any]:
    if not ban.is_banned:
        return {"banned": False}
    return {
        "banned": True,
        "reason": ban.reason or "#DISCONNECT_BANNED",
        "banType": ban.ban_type,
    }


async def check_player_ban(raw_id: str) -> PylonResponse:
    raw_id = raw_id.strip()
    if not raw_id:
        return PylonResponse(status_code=200, content={"banned": False})

    try:
        persona_id = int(raw_id)
    except ValueError:
        return PylonResponse(status_code=200, content={"banned": False})

    ban = await lookup_player_ban_by_persona_id(persona_id)
    return PylonResponse(status_code=200, content=_ban_response(ban))


async def bulk_check_bans(players: list[tuple[str, str | None]]) -> PylonResponse:
    persona_to_player: list[tuple[int, str, str | None]] = []
    for player_id, player_ip in players:
        try:
            persona_to_player.append((int(player_id), player_id, player_ip))
        except ValueError:
            continue

    if not persona_to_player:
        return PylonResponse(status_code=200, content={"bannedPlayers": []})

    bans = await lookup_bans_by_persona_ids([pid for pid, _, _ in persona_to_player])

    banned_players: list[dict[str, Any]] = []
    for persona_id, player_id, player_ip in persona_to_player:
        ban = bans.get(persona_id)
        if ban is None or not ban.is_banned:
            continue
        banned_players.append(
            {
                "id": player_id,
                "ip": player_ip or "",
                "reason": ban.reason or "#DISCONNECT_BANNED",
                "banType": ban.ban_type,
            }
        )

    return PylonResponse(status_code=200, content={"bannedPlayers": banned_players})


def get_eula() -> PylonResponse:
    return PylonResponse(
        status_code=200,
        content={
            "data": {
                "version": _EULA_VERSION,
                "language": _EULA_LANGUAGE,
                "contents": _EULA_CONTENTS,
            }
        },
    )


def add_server(*, ip: str | None, port: int | None, hidden: bool, checksum: int | None) -> PylonResponse:
    payload: dict[str, Any] = {
        "ip": ip or "",
        "port": port or 0,
    }
    if hidden:
        payload["token"] = f"selfhost:{checksum or 0}:{port or 0}"
    return PylonResponse(status_code=200, content=payload)
