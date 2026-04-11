"""DB helpers used by the Pylon master-server endpoints.

Kept separate from `steam_auth_service` (Steam Web API) and
`jwt_auth_service` (signing) so the HTTP layer can compose the three without
those modules taking on a Tortoise dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from shared_lib.models import BanRecord, Player, SteamAuthLog
from tortoise.exceptions import DoesNotExist


@dataclass(slots=True)
class BanLookupResult:
    is_banned: bool
    reason: str | None = None
    operator: str | None = None
    # 0 = connection ban (default), 1 = communication ban; matches
    # CBanSystem::Banned_t::BanType_e in r5r_sdk.
    ban_type: int = 0


async def write_steam_auth_log(
    *,
    steam_id: int,
    persona_name: str | None,
    server_endpoint: str | None,
    client_ip: str | None,
    success: bool,
    error_code: str | None = None,
) -> None:
    """Best-effort audit log write. Never raises into the request handler."""
    try:
        await SteamAuthLog.create(
            steam_id=steam_id,
            persona_name=persona_name,
            server_endpoint=server_endpoint,
            client_ip=client_ip,
            success=success,
            error_code=error_code,
        )
    except Exception as exc:  # pragma: no cover - audit log must not break auth
        logger.warning(f"Failed to write SteamAuthLog: {exc}")


async def lookup_player_ban_by_persona_id(persona_id: int) -> BanLookupResult:
    """Check if `persona_id` (NucleusID or SteamID64) maps to a banned Player.

    The bot's existing `Player.nucleus_id` historically holds NucleusIDs from
    LiveAPI events. SteamIDs may also be stored there in the future once
    Steam-only players get tied to Player rows; until then this lookup will
    simply miss for Steam-only users (returning not banned), which matches the
    intent of "permissive while we backfill".
    """
    try:
        player = await Player.get(nucleus_id=persona_id)
    except DoesNotExist:
        return BanLookupResult(is_banned=False)
    except Exception as exc:
        logger.warning(f"Player ban lookup failed for id={persona_id}: {exc}")
        return BanLookupResult(is_banned=False)

    if player.status != "banned":
        return BanLookupResult(is_banned=False)

    # Take the most recent ban record (if any) for the reason / operator detail.
    ban_record: BanRecord | None = (
        await BanRecord.filter(player=player).order_by("-created_at").first()
    )

    if ban_record is None:
        return BanLookupResult(
            is_banned=True,
            reason="banned",
            operator=None,
            ban_type=0,
        )

    return BanLookupResult(
        is_banned=True,
        reason=ban_record.reason,
        operator=ban_record.operator,
        ban_type=0,
    )
