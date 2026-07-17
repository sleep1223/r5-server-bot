from typing import Literal

from shared_lib.config import settings
from shared_lib.models import PlayerAccessOperation, UserBinding
from tortoise.expressions import Q

BindingRole = Literal["user", "admin", "super_admin"]


def _normalized_qqs(values: list[int]) -> set[str]:
    return {str(value).strip() for value in values if str(value).strip()}


def configured_role_for(platform: str, platform_uid: object) -> BindingRole | None:
    if platform != "qq":
        return None
    uid = str(platform_uid or "").strip()
    if uid in _normalized_qqs(settings.configured_super_admin_qqs):
        return "super_admin"
    if uid in _normalized_qqs(settings.configured_admin_qqs):
        return "admin"
    return None


def configured_flags_for(platform: str, platform_uid: object) -> tuple[bool, bool]:
    role = configured_role_for(platform, platform_uid)
    return role in {"admin", "super_admin"}, role == "super_admin"


def binding_role(binding: UserBinding) -> BindingRole:
    if binding.is_super_admin:
        return "super_admin"
    if binding.is_admin:
        return "admin"
    return "user"


def serialize_binding(binding: UserBinding) -> dict:
    player = binding.player
    return {
        "id": binding.id,
        "platform": binding.platform,
        "platform_uid": binding.platform_uid,
        "player_id": player.id,
        "player_name": player.name,
        "nucleus_id": player.nucleus_id,
        "is_admin": bool(binding.is_admin or binding.is_super_admin),
        "is_super_admin": bool(binding.is_super_admin),
        "role": binding_role(binding),
        "configured_role": configured_role_for(binding.platform, binding.platform_uid),
        "created_at": binding.created_at,
    }


async def apply_configured_roles() -> dict[str, int]:
    admin_qqs = _normalized_qqs(settings.configured_admin_qqs)
    super_admin_qqs = _normalized_qqs(settings.configured_super_admin_qqs)
    super_updated = 0
    admin_updated = 0
    if super_admin_qqs:
        super_updated = await UserBinding.filter(platform="qq", platform_uid__in=super_admin_qqs).update(is_admin=True, is_super_admin=True)
    admin_qqs -= super_admin_qqs
    if admin_qqs:
        admin_updated = await UserBinding.filter(platform="qq", platform_uid__in=admin_qqs, is_admin=False).update(is_admin=True)
    return {"admin": admin_updated, "super_admin": super_updated}


async def grant_admins_by_qqs(qqs: set[str]) -> dict[str, int]:
    normalized = {str(qq).strip() for qq in qqs if str(qq).strip()}
    excluded = _normalized_qqs(settings.milky_admin_group_grant_excluded_qqs)
    eligible = normalized - excluded
    if not eligible:
        return {"members": len(normalized), "bound": 0, "granted": 0, "already_admin": 0, "excluded": len(normalized & excluded)}

    bindings = await UserBinding.filter(platform="qq", platform_uid__in=eligible)
    grant_ids = [binding.id for binding in bindings if not binding.is_admin and not binding.is_super_admin]
    if grant_ids:
        await UserBinding.filter(id__in=grant_ids).update(is_admin=True)
    return {
        "members": len(normalized),
        "bound": len(bindings),
        "granted": len(grant_ids),
        "already_admin": len(bindings) - len(grant_ids),
        "excluded": len(normalized & excluded),
    }


async def list_bindings(
    *,
    q: str | None = None,
    platform: str | None = None,
    role: BindingRole | None = None,
    page_size: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    query = UserBinding.all().select_related("player")
    if q:
        filters = Q(platform_uid__icontains=q) | Q(player__name__icontains=q)
        if q.isdigit():
            filters |= Q(player__nucleus_id=int(q))
        query = query.filter(filters)
    if platform:
        query = query.filter(platform=platform)
    if role == "super_admin":
        query = query.filter(is_super_admin=True)
    elif role == "admin":
        query = query.filter(is_admin=True, is_super_admin=False)
    elif role == "user":
        query = query.filter(is_admin=False, is_super_admin=False)

    total = await query.count()
    bindings = await query.order_by("-id").offset(offset).limit(page_size)
    return [serialize_binding(binding) for binding in bindings], total


async def set_binding_role(
    *,
    binding_id: int,
    role: BindingRole,
    operator: UserBinding,
    remark: str | None = None,
) -> tuple[dict | None, str | None]:
    binding = await UserBinding.filter(id=binding_id).select_related("player").first()
    if not binding:
        return None, "未找到绑定记录"

    old_role = binding_role(binding)
    if old_role == "super_admin" and role != "super_admin":
        super_admin_count = await UserBinding.filter(is_super_admin=True).count()
        if super_admin_count <= 1:
            return None, "不能降级最后一个超级管理员"

    is_super_admin = role == "super_admin"
    is_admin = role in {"admin", "super_admin"}
    await UserBinding.filter(id=binding.id).update(is_admin=is_admin, is_super_admin=is_super_admin)
    binding.is_admin = is_admin  # type: ignore[assignment]
    binding.is_super_admin = is_super_admin  # type: ignore[assignment]

    operation = await PlayerAccessOperation.create(
        action="admin_set",
        target_type="binding",
        target_value=str(binding.id),
        normalized_target=f"{binding.platform}:{binding.platform_uid}",
        reason="ADMIN",
        remark=remark,
        operator=f"{operator.platform}:{operator.platform_uid}",
        player=binding.player,
        result={
            "binding_id": binding.id,
            "platform": binding.platform,
            "platform_uid": binding.platform_uid,
            "old_role": old_role,
            "new_role": role,
        },
    )
    return {"binding": serialize_binding(binding), "operation_id": operation.id}, None
