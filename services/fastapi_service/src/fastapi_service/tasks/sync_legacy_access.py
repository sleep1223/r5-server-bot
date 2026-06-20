from loguru import logger

from fastapi_service.services import player_access_service


async def sync_legacy_access_records_once() -> None:
    try:
        stats = await player_access_service.sync_legacy_access_records()
    except Exception as exc:  # pragma: no cover - defensive startup guard
        logger.error(f"legacy access 同步失败: {exc}")
        return

    logger.info(
        "legacy access 同步完成: "
        f"banned_checked={stats['banned_checked']}, "
        f"ban_rules_created={stats['ban_rules_created']}, "
        f"kick_checked={stats['kick_checked']}, "
        f"kick_notices_created={stats['kick_notices_created']}"
    )
