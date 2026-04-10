from shared_lib.models import TeamMember, TeamPost, UserBinding
from tortoise.transactions import in_transaction


async def _get_player_kd(player_id: int) -> float:
    """计算玩家总 KD。"""
    from shared_lib.models import PlayerKilled

    kills = await PlayerKilled.filter(attacker_id=player_id).count()
    deaths = await PlayerKilled.filter(victim_id=player_id).count()
    if deaths == 0:
        return float(kills)
    return round(kills / deaths, 2)


async def _binding_to_dict(binding: UserBinding, include_kd: bool = True) -> dict:
    if not hasattr(binding, "player") or binding.player is None:
        await binding.fetch_related("player")
    result = {
        "binding_id": binding.id,
        "platform": binding.platform,
        "platform_uid": binding.platform_uid,
        "player_id": binding.player.id,
        "player_name": binding.player.name,
    }
    if include_kd:
        result["kd"] = await _get_player_kd(binding.player.id)
    return result


async def _team_to_dict(team: TeamPost) -> dict:
    await team.fetch_related("members__user_binding__player", "creator__player")
    members = []
    for m in team.members:
        members.append({
            **(await _binding_to_dict(m.user_binding)),
            "role": m.role,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
        })
    return {
        "id": team.id,
        "creator": await _binding_to_dict(team.creator),
        "slots_needed": team.slots_needed,
        "slots_remaining": team.slots_needed - (len(members) - 1),  # 减去创建者
        "status": team.status,
        "members": members,
        "created_at": team.created_at.isoformat() if team.created_at else None,
    }


async def get_active_team(binding_id: int) -> TeamPost | None:
    """查询用户当前进行中的队伍。"""
    member = (
        await TeamMember
        .filter(
            user_binding_id=binding_id,
            team__status="open",
        )
        .prefetch_related("team")
        .first()
    )
    return member.team if member else None


async def create_team(binding_id: int, slots_needed: int) -> tuple[dict | None, str | None]:
    """创建组队。返回 (team_dict, error_msg)。"""
    if slots_needed not in (1, 2):
        return None, "缺人数只能是 1 或 2"

    active = await get_active_team(binding_id)
    if active:
        return None, f"你已有进行中的队伍 #{active.id}，请先取消或退出"

    async with in_transaction():
        team = await TeamPost.create(creator_id=binding_id, slots_needed=slots_needed)
        await TeamMember.create(team=team, user_binding_id=binding_id, role="creator")

    return await _team_to_dict(team), None


async def list_open_teams(page_size: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    """列出所有开放的队伍，按创建者 KD 排序。"""
    total = await TeamPost.filter(status="open").count()
    teams = await TeamPost.filter(status="open").prefetch_related("members__user_binding__player", "creator__player").order_by("-created_at").offset(offset).limit(page_size)

    results = []
    for team in teams:
        results.append(await _team_to_dict(team))

    # 按创建者 KD 降序排序
    results.sort(key=lambda t: t["creator"].get("kd", 0), reverse=True)
    return results, total


async def join_team(team_id: int, binding_id: int) -> tuple[dict | None, str | None]:
    """加入队伍。返回 (team_dict, error_msg)。"""
    active = await get_active_team(binding_id)
    if active:
        return None, f"你已有进行中的队伍 #{active.id}"

    async with in_transaction():
        team = await TeamPost.filter(id=team_id, status="open").select_for_update().first()
        if not team:
            return None, "队伍不存在或已关闭"

        if team.creator_id == binding_id:
            return None, "不能加入自己创建的队伍"

        member_count = await TeamMember.filter(team=team).count()
        max_members = team.slots_needed + 1  # slots_needed + creator
        if member_count >= max_members:
            return None, "队伍已满"

        await TeamMember.create(team=team, user_binding_id=binding_id, role="member")

        # 检查是否满员
        new_count = member_count + 1
        if new_count >= max_members:
            team.status = "full"
            await team.save()

    return await _team_to_dict(team), None


async def cancel_team(team_id: int, binding_id: int) -> tuple[bool, str | None]:
    """取消组队（仅队长）。"""
    team = await TeamPost.filter(id=team_id, status="open").first()
    if not team:
        return False, "队伍不存在或已关闭"
    if team.creator_id != binding_id:
        return False, "只有队长可以取消组队"

    team.status = "cancelled"
    await team.save()
    return True, None


async def leave_team(team_id: int, binding_id: int) -> tuple[bool, str | None]:
    """退出队伍（非队长）。"""
    team = await TeamPost.filter(id=team_id, status="open").first()
    if not team:
        return False, "队伍不存在或已关闭"
    if team.creator_id == binding_id:
        return False, "队长不能退出队伍，请使用取消组队"

    deleted = await TeamMember.filter(team=team, user_binding_id=binding_id).delete()
    if not deleted:
        return False, "你不在该队伍中"
    return True, None


async def get_team_detail(team_id: int) -> dict | None:
    """获取队伍详情。"""
    team = await TeamPost.filter(id=team_id).first()
    if not team:
        return None
    return await _team_to_dict(team)


async def get_full_team_members(team_id: int) -> list[dict]:
    """获取已满员队伍的所有成员信息（用于通知）。"""
    members = await TeamMember.filter(team_id=team_id).prefetch_related("user_binding__player")
    result = []
    for m in members:
        binding = m.user_binding
        result.append({
            "platform": binding.platform,
            "platform_uid": binding.platform_uid,
            "player_name": binding.player.name,
            "kd": await _get_player_kd(binding.player.id),
            "role": m.role,
        })
    return result


async def get_binding_by_platform(platform: str, platform_uid: str) -> UserBinding | None:
    """通过平台信息查找绑定。"""
    return await UserBinding.filter(platform=platform, platform_uid=platform_uid).prefetch_related("player").first()


async def find_binding_by_player_name(player_name: str) -> UserBinding | None:
    """通过玩家名查找绑定（用于邀请）。"""
    binding = await UserBinding.filter(player__name__icontains=player_name).prefetch_related("player").first()
    return binding


async def invite_player(team_id: int, creator_binding_id: int, target_player_name: str) -> tuple[dict | None, str | None]:
    """邀请玩家加入队伍。返回 (target_binding_dict, error_msg)。"""
    team = await TeamPost.filter(id=team_id, status="open").first()
    if not team:
        return None, "队伍不存在或已关闭"
    if team.creator_id != creator_binding_id:
        return None, "只有队长可以邀请"

    member_count = await TeamMember.filter(team=team).count()
    max_members = team.slots_needed + 1
    if member_count >= max_members:
        return None, "队伍已满"

    target_binding = await find_binding_by_player_name(target_player_name)
    if not target_binding:
        return None, f"未找到已绑定的玩家: {target_player_name}"

    active = await get_active_team(target_binding.id)
    if active:
        return None, f"玩家 {target_binding.player.name} 已在队伍 #{active.id} 中"

    return await _binding_to_dict(target_binding), None


async def accept_invite(team_id: int, binding_id: int) -> tuple[dict | None, str | None]:
    """接受邀请加入队伍（与 join_team 逻辑相同）。"""
    return await join_team(team_id, binding_id)
