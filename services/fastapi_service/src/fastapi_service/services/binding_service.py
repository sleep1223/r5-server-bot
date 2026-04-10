import secrets

from shared_lib.models import Player, UserBinding
from tortoise.exceptions import IntegrityError


async def _find_player(query: str) -> tuple[Player | None, str | None]:
    """通过 nucleus_id 或名称查找玩家。返回 (player, error_msg)。"""
    # 尝试按 nucleus_id 精确匹配
    if query.isdigit():
        player = await Player.filter(nucleus_id=int(query)).first()
        if player:
            return player, None

    # 按名称模糊匹配
    players = await Player.filter(name__icontains=query).limit(5)
    if not players:
        return None, f"未找到玩家: {query}"
    if len(players) > 1:
        names = ", ".join(p.name for p in players)
        return None, f"匹配到多个玩家({names})，请提供更精确的昵称或ID"

    return players[0], None


async def bind_player(platform: str, platform_uid: str, player_query: str) -> tuple[dict | None, str | None]:
    """绑定平台账号到游戏玩家。返回 (binding_dict, error_msg)。"""
    player, err = await _find_player(player_query)
    if err:
        return None, err
    app_key = secrets.token_urlsafe(32)

    try:
        binding = await UserBinding.create(
            platform=platform,
            platform_uid=platform_uid,
            player=player,
            app_key=app_key,
        )
    except IntegrityError:
        existing = await UserBinding.filter(platform=platform, platform_uid=platform_uid).prefetch_related("player").first()
        if existing:
            return None, f"你已绑定玩家: {existing.player.name}，请先解绑"
        return None, "绑定失败，请稍后重试"

    assert player is not None
    return {
        "id": binding.id,
        "platform": binding.platform,
        "platform_uid": binding.platform_uid,
        "player_id": player.id,
        "player_name": player.name,
        "app_key": binding.app_key,
    }, None


async def unbind(platform: str, platform_uid: str) -> bool:
    """解除绑定。"""
    deleted = await UserBinding.filter(platform=platform, platform_uid=platform_uid).delete()
    return deleted > 0


async def get_binding(platform: str, platform_uid: str) -> dict | None:
    """查询绑定信息。"""
    binding = await UserBinding.filter(platform=platform, platform_uid=platform_uid).prefetch_related("player").first()
    if not binding:
        return None
    return {
        "id": binding.id,
        "platform": binding.platform,
        "platform_uid": binding.platform_uid,
        "player_id": binding.player.id,
        "player_name": binding.player.name,
        "app_key": binding.app_key,
    }


async def get_binding_by_app_key(app_key: str) -> dict | None:
    """通过 AppKey 查询绑定信息。"""
    binding = await UserBinding.filter(app_key=app_key).prefetch_related("player").first()
    if not binding:
        return None
    return {
        "id": binding.id,
        "platform": binding.platform,
        "platform_uid": binding.platform_uid,
        "player_id": binding.player.id,
        "player_name": binding.player.name,
        "nucleus_id": binding.player.nucleus_id,
    }
