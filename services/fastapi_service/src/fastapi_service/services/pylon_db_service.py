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


async def lookup_bans_by_persona_ids(persona_ids: list[int]) -> dict[int, BanLookupResult]:
    """批量版本：单查询取出所有 banned 玩家及其最新 BanRecord。

    返回的 dict 只包含 banned 玩家；调用方对未命中的 id 视作未封禁。
    用于替代 ``lookup_player_ban_by_persona_id`` 的 N+1 调用。
    """
    if not persona_ids:
        return {}
    try:
        banned_players = await Player.filter(
            nucleus_id__in=persona_ids, status="banned"
        )
    except Exception as exc:
        logger.warning(f"Bulk ban lookup failed: {exc}")
        return {}

    if not banned_players:
        return {}

    by_id = {p.id: p for p in banned_players}
    # 一次性取所有相关玩家的 BanRecord，按 created_at 倒序，取首条作为最新
    record_rows = (
        await BanRecord.filter(player_id__in=list(by_id.keys()))
        .order_by("-created_at")
        .values("player_id", "reason", "operator")
    )
    latest_record_by_player: dict[int, dict] = {}
    for r in record_rows:
        pid = r["player_id"]
        if pid not in latest_record_by_player:
            latest_record_by_player[pid] = r

    out: dict[int, BanLookupResult] = {}
    for p in banned_players:
        if not p.nucleus_id:
            continue
        try:
            persona_int = int(p.nucleus_id)
        except (TypeError, ValueError):
            continue
        rec = latest_record_by_player.get(p.id)
        if rec is None:
            out[persona_int] = BanLookupResult(is_banned=True, reason="banned", operator=None, ban_type=0)
        else:
            out[persona_int] = BanLookupResult(
                is_banned=True, reason=rec["reason"], operator=rec["operator"], ban_type=0
            )
    return out


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
