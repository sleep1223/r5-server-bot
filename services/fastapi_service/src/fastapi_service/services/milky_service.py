from typing import Any

import httpx
from shared_lib.config import settings


async def _post(endpoint: str, payload: dict[str, Any]) -> Any:
    base_url = settings.milky_api_base_url.strip()
    if not base_url:
        raise ValueError("未配置 Milky API 地址")

    headers: dict[str, str] = {}
    if settings.milky_access_token:
        headers["Authorization"] = f"Bearer {settings.milky_access_token}"

    timeout = max(float(settings.milky_request_timeout_seconds), 1.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        body = response.json()

    if not isinstance(body, dict) or body.get("status") != "ok" or body.get("retcode") != 0:
        raise ValueError(f"Milky 返回失败: status={body.get('status') if isinstance(body, dict) else None}, retcode={body.get('retcode') if isinstance(body, dict) else None}")
    return body.get("data")


async def get_group_member_list(group_id: int, *, no_cache: bool = False) -> list[dict[str, Any]]:
    data = await _post("get_group_member_list", {"group_id": group_id, "no_cache": no_cache})
    members = data.get("members") if isinstance(data, dict) else None
    if not isinstance(members, list):
        raise ValueError("Milky 返回缺少 data.members 列表")
    return [member for member in members if isinstance(member, dict)]


async def send_private_message(user_id: int, text: str) -> Any:
    return await _post(
        "send_private_message",
        {
            "user_id": user_id,
            "message": [
                {
                    "type": "text",
                    "data": {"text": text},
                }
            ],
        },
    )
