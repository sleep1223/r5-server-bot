import time
from math import ceil

from fastapi import APIRouter, Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from shared_lib.config import settings

from fastapi_service.core.auth import security_scheme
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, paginated, success
from fastapi_service.core.utils import check_is_admin
from fastapi_service.services import admin_service

from ..deps import Pagination, get_pagination

router = APIRouter()

SELF_UNBAN_IP_RATE_LIMIT_SECONDS = 60.0
_SELF_UNBAN_IP_LAST_SUCCESS: dict[str, float] = {}


class SelfUnbanBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    player_name: str | None = None
    nucleus_id: int | None = None
    operation_id: int | None = None
    confirmation_text: str = Field(..., min_length=1)


def _request_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        first_ip = forwarded_for.split(",", 1)[0].strip()
        if first_ip:
            return first_ip

    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip

    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _prune_self_unban_ip_rate_limit(now: float) -> None:
    expired_ips = [
        ip
        for ip, last_success_at in _SELF_UNBAN_IP_LAST_SUCCESS.items()
        if now - last_success_at >= SELF_UNBAN_IP_RATE_LIMIT_SECONDS
    ]
    for ip in expired_ips:
        _SELF_UNBAN_IP_LAST_SUCCESS.pop(ip, None)


def _reserve_self_unban_ip_slot(ip: str, *, now: float | None = None) -> tuple[float | None, int]:
    current = time.monotonic() if now is None else now
    _prune_self_unban_ip_rate_limit(current)

    last_success_at = _SELF_UNBAN_IP_LAST_SUCCESS.get(ip)
    if last_success_at is not None:
        remaining = SELF_UNBAN_IP_RATE_LIMIT_SECONDS - (current - last_success_at)
        if remaining > 0:
            return None, ceil(remaining)

    _SELF_UNBAN_IP_LAST_SUCCESS[ip] = current
    return current, 0


def _release_self_unban_ip_slot(ip: str, reserved_at: float | None) -> None:
    if reserved_at is not None and _SELF_UNBAN_IP_LAST_SUCCESS.get(ip) == reserved_at:
        _SELF_UNBAN_IP_LAST_SUCCESS.pop(ip, None)


@router.get("/bans")
async def get_ban_list(
    q: str | None = Query(None, description="精确搜索玩家名或 Nucleus ID"),
    player_name: str | None = Query(None, description="精确搜索玩家名"),
    nucleus_id: int | None = Query(None, description="精确搜索 Nucleus ID"),
    pg: Pagination = Depends(get_pagination),
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
):
    """获取封禁记录列表。"""
    is_admin = check_is_admin(credentials, settings.fastapi_access_tokens)

    results, total = await admin_service.list_bans(
        page_size=pg.page_size,
        offset=pg.offset,
        is_admin=is_admin,
        player_query=q,
        player_name=player_name,
        nucleus_id=nucleus_id,
    )

    return paginated(data=results, total=total, msg="封禁列表已获取")


@router.post("/bans/self-unban")
async def self_unban_player(request: Request, body: SelfUnbanBody):
    client_ip = _request_ip(request)
    reserved_at, retry_after_seconds = _reserve_self_unban_ip_slot(client_ip)
    if retry_after_seconds > 0:
        return error(
            ErrorCode.INVALID_REASON,
            f"同一 IP 每分钟只能自助解封一次，请 {retry_after_seconds} 秒后再试",
            retry_after_seconds=retry_after_seconds,
        )

    data, err = await admin_service.self_unban_player(
        player_name=body.player_name,
        nucleus_id=body.nucleus_id,
        operation_id=body.operation_id,
        confirmation_text=body.confirmation_text,
    )
    if err:
        _release_self_unban_ip_slot(client_ip, reserved_at)
        return err
    return success(data=data, msg="已确认规则，自助解封已提交", retry_after_seconds=int(SELF_UNBAN_IP_RATE_LIMIT_SECONDS))
