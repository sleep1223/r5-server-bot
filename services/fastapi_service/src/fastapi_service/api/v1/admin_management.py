from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from shared_lib.models import Player, UserBinding

from fastapi_service.core.auth import is_admin_binding, is_super_admin_binding, verify_admin_app_key, verify_super_admin_app_key, verify_token
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, paginated, success
from fastapi_service.services import admin_management_service, player_access_service, player_service

from ..deps import Pagination, get_pagination

router = APIRouter(prefix="/admin", tags=["r5-admin"], dependencies=[Depends(verify_admin_app_key)])
bot_router = APIRouter(prefix="/admin/bot", tags=["r5-admin-bot"], dependencies=[Depends(verify_token)])


def _require_super_admin(binding: UserBinding, detail: str = "需要超级管理员权限") -> None:
    if not is_super_admin_binding(binding):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


async def _verify_bot_operator(platform: str, platform_uid: str) -> UserBinding:
    normalized_platform = (platform or "qq").strip().lower()
    normalized_uid = str(platform_uid or "").strip()
    if not normalized_uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="缺少操作者 QQ")

    binding = await UserBinding.filter(platform=normalized_platform, platform_uid=normalized_uid).prefetch_related("player").first()
    if not binding:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="操作者 QQ 未绑定玩家")
    if not is_admin_binding(binding):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="操作者绑定玩家不是管理员")
    return binding


def _bot_operator_name(binding: UserBinding) -> str:
    return f"qq:{binding.platform_uid}"


def _public_allow_access() -> dict[str, Any]:
    return {
        "allow": True,
        "reason": None,
        "rule_id": None,
        "rule_type": None,
        "server_scope": None,
        "server_id": None,
        "source": "default_allow",
    }


def _is_deny_access(value: object) -> bool:
    return isinstance(value, dict) and value.get("allow") is False


def _is_deny_rule(value: object) -> bool:
    return isinstance(value, dict) and value.get("action") == "deny"


def _sanitize_access_for_regular_admin(value: object) -> object:
    return value if _is_deny_access(value) else _public_allow_access()


def _sanitize_access_trace_for_regular_admin(trace: object) -> object:
    if not isinstance(trace, dict):
        return trace

    checks = []
    for check in trace.get("checks") or []:
        if not isinstance(check, dict):
            continue
        if _is_deny_rule(check.get("rule")) or isinstance(check.get("notice"), dict) or _is_deny_access(check.get("decision")):
            checks.append(check)

    return {
        **trace,
        "decision": _sanitize_access_for_regular_admin(trace.get("decision")),
        "checks": checks,
        "matched_rules": [item for item in trace.get("matched_rules") or [] if _is_deny_access(item)],
    }


def _sanitize_player_payload_for_regular_admin(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    if "access" in sanitized:
        sanitized["access"] = _sanitize_access_for_regular_admin(sanitized["access"])
    if "access_rules" in sanitized:
        sanitized["access_rules"] = [rule for rule in sanitized["access_rules"] if _is_deny_rule(rule)]
    if "access_trace" in sanitized:
        sanitized["access_trace"] = _sanitize_access_trace_for_regular_admin(sanitized["access_trace"])
    return sanitized


class ScopedServerBody(BaseModel):
    server_scope: Literal["global", "server"] = "global"
    server_id: str | None = None
    server_key: str | None = None
    server_host: str | None = None
    server_port: int | None = None


class PlayerActionBody(ScopedServerBody):
    reason: str = "RULES"
    sync_player_ip: bool = False
    remark: str | None = None
    duration_seconds: int | None = Field(default=None, gt=0)


class UnbanBody(ScopedServerBody):
    remark: str | None = None


class PlayerAdminBody(BaseModel):
    is_admin: bool
    remark: str | None = None


class AccessActionBody(ScopedServerBody):
    target_type: Literal["player", "uid", "ip", "cidr", "country", "region"]
    target_value: int | str
    reason: str = "RULES"
    sync_player_ip: bool = False
    remark: str | None = None
    duration_seconds: int | None = Field(default=None, gt=0)


class BotAccessActionBody(AccessActionBody):
    operator_platform: Literal["qq"] = "qq"
    operator_uid: str


class BotUnbanBody(UnbanBody):
    operator_platform: Literal["qq"] = "qq"
    operator_uid: str


class AccessPreviewBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    player_identifier: int | str | None = None
    uid: int | str | None = None
    ip: str | None = None
    server_id: str | None = None
    country: str | None = None
    region: str | None = None


class AccessRuleCreateBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    rule_type: Literal["uid", "ip", "cidr", "geo", "country", "region", "geo_policy"]
    action: Literal["allow", "deny"]
    value: str
    server_scope: Literal["global", "server"] = "global"
    server_id: str | None = None
    reason: str | None = None
    remark: str | None = None
    rule_id: str | None = None
    source_action: str | None = None
    expires_at: datetime | None = None
    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    player_identifier: int | str | None = None


class AccessRuleUpdateBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    rule_type: Literal["uid", "ip", "cidr", "geo", "country", "region", "geo_policy"] | None = None
    action: Literal["allow", "deny"] | None = None
    value: str | None = None
    server_scope: Literal["global", "server"] | None = None
    server_id: str | None = None
    reason: str | None = None
    remark: str | None = None
    rule_id: str | None = None
    operator: str | None = None
    source_action: str | None = None
    expires_at: datetime | None = None
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0)


@router.get("/players")
async def admin_list_players(
    q: str | None = None,
    status: Literal["online", "offline", "ban", "kick", "banned", "kicked"] | None = None,
    nucleus_id: int | None = None,
    ip: str | None = None,
    country: str | None = None,
    region: str | None = None,
    is_admin: bool | None = None,
    access_server_id: str | None = None,
    pg: Pagination = Depends(get_pagination),
    binding: UserBinding = Depends(verify_admin_app_key),
):
    try:
        items, total = await admin_management_service.list_players(
            q=q,
            status=status,
            nucleus_id=nucleus_id,
            ip=ip,
            country=country,
            region=region,
            is_admin=is_admin,
            access_server_id=access_server_id,
            page_size=pg.page_size,
            offset=pg.offset,
        )
    except ValueError as exc:
        return error(ErrorCode.INVALID_REASON, str(exc))
    if not is_super_admin_binding(binding):
        items = [_sanitize_player_payload_for_regular_admin(item) for item in items]
    return paginated(data=items, total=total, msg="管理员玩家列表已获取")


@router.get("/players/{identifier}")
async def admin_get_player(identifier: int | str, access_server_id: str | None = None, binding: UserBinding = Depends(verify_admin_app_key)):
    player, err = await player_service.get_player_by_identifier(identifier)
    if err:
        return err
    assert player is not None
    data = await admin_management_service.serialize_player_detail(
        player,
        access_server_id=access_server_id,
        include_history=True,
    )
    if not is_super_admin_binding(binding):
        data = _sanitize_player_payload_for_regular_admin(data)
    return success(data=data, msg="管理员玩家详情已获取")


@router.get("/players/{identifier}/access-matches")
async def admin_get_player_access_matches(identifier: int | str, access_server_id: str | None = None, binding: UserBinding = Depends(verify_admin_app_key)):
    _require_super_admin(binding)

    player, err = await player_service.get_player_by_identifier(identifier)
    if err:
        return err
    assert player is not None
    data = await player_access_service.trace_player_access(
        uid=player.nucleus_id,
        ip=player.ip,
        server_id=access_server_id,
        player=player,
        country=player.country,
        region=player.region,
    )
    return success(data=data, msg="玩家准入匹配结果已获取")


@router.patch("/players/{identifier}/admin", dependencies=[Depends(verify_super_admin_app_key)])
async def admin_set_player_admin(identifier: int | str, body: PlayerAdminBody):
    data, err = await admin_management_service.set_player_admin(
        identifier=identifier,
        is_admin=body.is_admin,
        operator_name="super_admin",
        remark=body.remark,
    )
    if err:
        return err
    return success(data=data, msg="玩家管理员标记已更新")


@router.post("/players/{identifier}/ban", dependencies=[Depends(verify_super_admin_app_key)])
async def admin_ban_player(identifier: int | str, body: PlayerActionBody):
    data, err = await admin_management_service.ban_player(
        identifier=identifier,
        reason=body.reason,
        operator_name="admin",
        server_scope=body.server_scope,
        server_id=body.server_id,
        server_key=body.server_key,
        server_host=body.server_host,
        server_port=body.server_port,
        sync_player_ip=body.sync_player_ip,
        remark=body.remark,
        duration_seconds=body.duration_seconds,
    )
    if err:
        return err
    return success(data=data, msg="管理员封禁已提交")


@router.post("/players/{identifier}/unban", dependencies=[Depends(verify_super_admin_app_key)])
async def admin_unban_player(identifier: int | str, body: UnbanBody):
    data, err = await admin_management_service.unban_player(
        identifier=identifier,
        operator_name="admin",
        server_scope=body.server_scope,
        server_id=body.server_id,
        server_key=body.server_key,
        server_host=body.server_host,
        server_port=body.server_port,
        remark=body.remark,
    )
    if err:
        return err
    return success(data=data, msg="管理员解封已提交")


@router.post("/players/{identifier}/kick")
async def admin_kick_player(identifier: int | str, body: PlayerActionBody, binding: UserBinding = Depends(verify_admin_app_key)):
    if body.sync_player_ip:
        _require_super_admin(binding, "普通管理员不能同步玩家 IP 黑名单")

    data, err = await admin_management_service.kick_player(
        identifier=identifier,
        reason=body.reason,
        operator_name="admin",
        server_scope=body.server_scope,
        server_id=body.server_id,
        server_key=body.server_key,
        server_host=body.server_host,
        server_port=body.server_port,
        sync_player_ip=body.sync_player_ip,
        remark=body.remark,
        duration_seconds=body.duration_seconds,
    )
    if err:
        return err
    return success(data=data, msg="管理员踢出已提交")


@router.post("/access-actions/{action}")
async def admin_apply_access_action(action: Literal["ban", "kick"], body: AccessActionBody, binding: UserBinding = Depends(verify_admin_app_key)):
    if not is_super_admin_binding(binding) and (action != "kick" or body.target_type != "player" or body.sync_player_ip):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="普通管理员只能踢出玩家")

    data, err = await admin_management_service.apply_access_action(
        action=action,
        target_type=body.target_type,
        target_value=body.target_value,
        reason=body.reason,
        operator_name="admin",
        server_scope=body.server_scope,
        server_id=body.server_id,
        server_key=body.server_key,
        server_host=body.server_host,
        server_port=body.server_port,
        sync_player_ip=body.sync_player_ip,
        remark=body.remark,
        duration_seconds=body.duration_seconds,
    )
    if err:
        return err
    action_label = {"ban": "封禁", "kick": "踢出"}.get(action, action)
    return success(data=data, msg=f"管理员{action_label}操作已提交")


@bot_router.post("/access-actions/{action}")
async def admin_bot_apply_access_action(action: Literal["ban", "kick"], body: BotAccessActionBody):
    binding = await _verify_bot_operator(body.operator_platform, body.operator_uid)
    if not is_super_admin_binding(binding) and (action != "kick" or body.target_type != "player" or body.sync_player_ip):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="普通管理员只能踢出玩家")

    data, err = await admin_management_service.apply_access_action(
        action=action,
        target_type=body.target_type,
        target_value=body.target_value,
        reason=body.reason,
        operator_name=_bot_operator_name(binding),
        server_scope=body.server_scope,
        server_id=body.server_id,
        server_key=body.server_key,
        server_host=body.server_host,
        server_port=body.server_port,
        sync_player_ip=body.sync_player_ip,
        remark=body.remark,
        duration_seconds=body.duration_seconds,
    )
    if err:
        return err
    action_label = {"ban": "封禁", "kick": "踢出"}.get(action, action)
    return success(data=data, msg=f"Bot {action_label}操作已提交")


@bot_router.post("/players/{identifier}/unban")
async def admin_bot_unban_player(identifier: int | str, body: BotUnbanBody):
    binding = await _verify_bot_operator(body.operator_platform, body.operator_uid)
    _require_super_admin(binding)

    data, err = await admin_management_service.unban_player(
        identifier=identifier,
        operator_name=_bot_operator_name(binding),
        server_scope=body.server_scope,
        server_id=body.server_id,
        server_key=body.server_key,
        server_host=body.server_host,
        server_port=body.server_port,
        remark=body.remark,
    )
    if err:
        return err
    return success(data=data, msg="Bot 解封已提交")


@router.get("/access-rules")
async def admin_list_access_rules(
    q: str | None = None,
    rule_type: Literal["uid", "ip", "cidr", "geo", "country", "region", "geo_policy"] | None = None,
    action: Literal["allow", "deny"] | None = None,
    server_scope: Literal["global", "server"] | None = None,
    server_id: str | None = None,
    enabled: bool | None = None,
    pg: Pagination = Depends(get_pagination),
    binding: UserBinding = Depends(verify_admin_app_key),
):
    _require_super_admin(binding)

    items, total = await player_access_service.list_access_rules(
        q=q,
        rule_type=rule_type,
        action=action,
        server_scope=server_scope,
        server_id=server_id,
        enabled=enabled,
        page_size=pg.page_size,
        offset=pg.offset,
    )
    return paginated(data=items, total=total, msg="准入规则已获取")


@router.post("/access-rules/preview", dependencies=[Depends(verify_super_admin_app_key)])
async def admin_preview_access_rules(body: AccessPreviewBody):
    player = None
    if body.player_identifier is not None:
        player, err = await player_service.get_player_by_identifier(body.player_identifier)
        if err:
            return err

    data = await player_access_service.trace_player_access(
        uid=body.uid if body.uid is not None else (player.nucleus_id if player else None),
        ip=body.ip if body.ip is not None else (player.ip if player else None),
        server_id=body.server_id,
        player=player,
        country=body.country if body.country is not None else (player.country if player else None),
        region=body.region if body.region is not None else (player.region if player else None),
    )
    return success(data=data, msg="准入规则预览已生成")


@router.post("/access-rules", dependencies=[Depends(verify_super_admin_app_key)])
async def admin_create_access_rule(body: AccessRuleCreateBody):
    player = None
    if body.player_identifier is not None:
        player, err = await player_service.get_player_by_identifier(body.player_identifier)
        if err:
            return err

    try:
        rule = await player_access_service.create_access_rule(
            rule_type=body.rule_type,
            action=body.action,
            value=body.value,
            server_scope=body.server_scope,
            server_id=body.server_id,
            reason=body.reason,
            remark=body.remark,
            rule_id=body.rule_id,
            operator="admin",
            source_action=body.source_action,
            expires_at=body.expires_at,
            enabled=body.enabled,
            priority=body.priority,
            player=player,
        )
    except ValueError as exc:
        return error(ErrorCode.INVALID_REASON, str(exc))

    return success(data=player_access_service.serialize_access_rule(rule), msg="准入规则已创建")


@router.get("/access-rules/{rule_db_id}")
async def admin_get_access_rule(rule_db_id: int, binding: UserBinding = Depends(verify_admin_app_key)):
    _require_super_admin(binding)

    rule = await player_access_service.get_access_rule(rule_db_id)
    if not rule:
        return error(ErrorCode.SERVER_NOT_FOUND, f"未找到准入规则: {rule_db_id}")
    return success(data=player_access_service.serialize_access_rule(rule), msg="准入规则已获取")


@router.patch("/access-rules/{rule_db_id}", dependencies=[Depends(verify_super_admin_app_key)])
async def admin_update_access_rule(rule_db_id: int, body: AccessRuleUpdateBody):
    rule = await player_access_service.get_access_rule(rule_db_id)
    if not rule:
        return error(ErrorCode.SERVER_NOT_FOUND, f"未找到准入规则: {rule_db_id}")

    updates = body.model_dump(exclude_unset=True)
    try:
        updated = await player_access_service.update_access_rule(rule, **updates)
    except ValueError as exc:
        return error(ErrorCode.INVALID_REASON, str(exc))

    return success(data=player_access_service.serialize_access_rule(updated), msg="准入规则已更新")


@router.delete("/access-rules/{rule_db_id}", dependencies=[Depends(verify_super_admin_app_key)])
async def admin_disable_access_rule(rule_db_id: int):
    rule = await player_access_service.get_access_rule(rule_db_id)
    if not rule:
        return error(ErrorCode.SERVER_NOT_FOUND, f"未找到准入规则: {rule_db_id}")
    disabled = await player_access_service.disable_access_rule(rule)
    return success(data=player_access_service.serialize_access_rule(disabled), msg="准入规则已禁用")


@router.get("/access-operations", dependencies=[Depends(verify_super_admin_app_key)])
async def admin_list_access_operations(
    action: str | None = None,
    target_type: str | None = None,
    q: str | None = None,
    player_id: int | None = None,
    server_scope: Literal["global", "server"] | None = None,
    server_id: str | None = None,
    pg: Pagination = Depends(get_pagination),
):
    items, total = await player_access_service.list_access_operations(
        action=action,
        target_type=target_type,
        q=q,
        player_id=player_id,
        server_scope=server_scope,
        server_id=server_id,
        page_size=pg.page_size,
        offset=pg.offset,
    )
    return paginated(data=items, total=total, msg="准入操作记录已获取")


@router.get("/access-notices", dependencies=[Depends(verify_super_admin_app_key)])
async def admin_list_access_notices(
    uid: str | None = None,
    requires_ack: bool | None = None,
    acknowledged: bool | None = None,
    server_scope: Literal["global", "server"] | None = None,
    server_id: str | None = None,
    pg: Pagination = Depends(get_pagination),
):
    items, total = await player_access_service.list_access_notices(
        uid=uid,
        requires_ack=requires_ack,
        acknowledged=acknowledged,
        server_scope=server_scope,
        server_id=server_id,
        page_size=pg.page_size,
        offset=pg.offset,
    )
    return paginated(data=items, total=total, msg="准入通知已获取")


@router.post("/access-notices/{notice_id}/ack", dependencies=[Depends(verify_super_admin_app_key)])
async def admin_ack_access_notice(notice_id: int):
    notice = await player_access_service.get_access_notice(notice_id)
    if not notice:
        return error(ErrorCode.SERVER_NOT_FOUND, f"未找到准入通知: {notice_id}")

    player = None
    notice_player_id = getattr(notice, "player_id", None)
    if notice_player_id:
        player = await Player.get_or_none(id=notice_player_id)

    operation = await player_access_service.create_access_operation(
        action="ack",
        target_type="uid",
        target_value=notice.uid,
        normalized_target=notice.uid,
        server_scope=notice.server_scope,
        server_id=notice.server_id,
        reason=notice.reason,
        operator="admin",
        player=player,
        result={"notice_id": notice.id},
        linked_rule_ids=[f"kick_notice:{notice.id}"],
    )
    updated = await player_access_service.acknowledge_access_notice(notice)
    return success(
        data={
            "notice": player_access_service.serialize_access_notice(updated),
            "operation": player_access_service.serialize_access_operation(operation),
        },
        msg="准入通知已确认",
    )
