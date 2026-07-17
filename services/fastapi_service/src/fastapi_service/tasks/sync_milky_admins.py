import asyncio

import httpx
from loguru import logger
from shared_lib.config import settings

from fastapi_service.services.binding_role_service import grant_admins_by_qqs


def _enabled() -> bool:
    return bool(settings.milky_api_base_url.strip() and settings.milky_admin_group_id > 0)


async def sync_milky_admins_once() -> dict[str, int] | None:
    if not _enabled():
        return None

    url = f"{settings.milky_api_base_url.rstrip('/')}/get_group_member_list"
    headers: dict[str, str] = {}
    if settings.milky_access_token:
        headers["Authorization"] = f"Bearer {settings.milky_access_token}"

    timeout = max(float(settings.milky_request_timeout_seconds), 1.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            headers=headers,
            json={"group_id": settings.milky_admin_group_id, "no_cache": False},
        )
        response.raise_for_status()
        payload = response.json()

    if not isinstance(payload, dict) or payload.get("status") != "ok" or payload.get("retcode") != 0:
        raise ValueError(f"Milky 返回失败: status={payload.get('status') if isinstance(payload, dict) else None}, retcode={payload.get('retcode') if isinstance(payload, dict) else None}")
    data = payload.get("data")
    members = data.get("members") if isinstance(data, dict) else None
    if not isinstance(members, list):
        raise ValueError("Milky 返回缺少 data.members 列表")

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
