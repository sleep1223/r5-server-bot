import asyncio

import httpx
from loguru import logger
from shared_lib.config import settings

from fastapi_service.services.binding_role_service import grant_admins_by_qqs
from fastapi_service.services.milky_service import get_group_member_list


def _enabled() -> bool:
    return bool(settings.milky_api_base_url.strip() and settings.milky_admin_group_id > 0)


async def sync_milky_admins_once() -> dict[str, int] | None:
    if not _enabled():
        return None

    members = await get_group_member_list(settings.milky_admin_group_id)

    qqs = {str(member.get("user_id") or "").strip() for member in members if isinstance(member, dict) and str(member.get("user_id") or "").strip()}
    return await grant_admins_by_qqs(qqs)


async def milky_admin_sync_task() -> None:
    if not _enabled():
        logger.info("Milky 管理群同步未启用")
        return

    interval = max(int(settings.milky_admin_sync_interval_seconds), 60)
    logger.info(f"Milky 管理群同步任务已启动: group_id={settings.milky_admin_group_id}, interval={interval}s")
    while True:
        try:
            summary = await sync_milky_admins_once()
            logger.info(f"Milky 管理群同步完成: {summary}")
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(f"Milky 管理群同步失败: {exc}")
        except Exception:
            logger.exception("Milky 管理群同步异常")
        await asyncio.sleep(interval)
