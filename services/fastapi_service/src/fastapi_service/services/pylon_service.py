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
from fastapi_service.services.player_access_service import action_reason_text
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
_EULA_CONTENTS = "连接到此服务器集群即表示你同意遵守服务器规则。此主服务器由 r5-server-bot 项目运营，与 Electronic Arts 或 Respawn Entertainment 无关联。"


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
        logger.warning(f"Steam ticket 校验失败: user_id={user_id}, code={exc.code}")
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
        logger.exception("Steam ticket 校验时出现未预期异常")
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
        logger.info(f"Steam ID 不匹配: 客户端声明 {user_id}, 校验结果 {steam.steamid}; 将使用校验后的 id")
        user_id = steam.steamid

    ban = await lookup_player_ban_by_persona_id(int(user_id))
    if ban.is_banned:
        ban_reason = action_reason_text("ban", ban.reason)
        logger.info(f"Steam 鉴权拦截已封禁玩家: user_id={user_id}, reason={ban_reason}")
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
            f"error: account banned ({ban_reason})",
        )

    display_name = steam.persona or steam_username or "steam_user"
    try:
        token = create_auth_token(user_id, display_name, server_endpoint)
    except JwtKeyError as exc:
        logger.error(f"JWT 签名失败: {exc}")
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
        logger.error(f"无法提供 JWT keyinfo: {exc}")
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
        "reason": action_reason_text("ban", ban.reason),
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
        banned_players.append({
            "id": player_id,
            "ip": player_ip or "",
            "reason": action_reason_text("ban", ban.reason),
            "banType": ban.ban_type,
        })

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


def _server_address_key(ip: str | None, port: int | None) -> str | None:
    host = str(ip or "").strip()
    try:
        port_int = int(port or 0)
    except (TypeError, ValueError):
        port_int = 0
    if not host or not port_int:
        return None
    return f"{host}:{port_int}"


def add_server(*, ip: str | None, port: int | None, hidden: bool, checksum: int | None) -> PylonResponse:
    payload: dict[str, Any] = {
        "ip": ip or "",
        "port": port or 0,
    }
    access_server_id = _server_address_key(ip, port)
    if access_server_id:
        payload["accessServerId"] = access_server_id
    if hidden:
        payload["token"] = f"selfhost:{checksum or 0}:{port or 0}"
    return PylonResponse(status_code=200, content=payload)
