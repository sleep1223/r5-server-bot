from nonebot import on_command as _on_command
from nonebot_plugin_access_control_api.service import create_plugin_service

# AC Service
# 使用 create_plugin_service 创建服务
r5_service = create_plugin_service("r5")

FRIEND_HINT = "👉 先添加机器人为好友，然后私信发送: /绑定 <游戏昵称>"
BINDING_GUIDE = f"❌ 请先绑定游戏账号\n{FRIEND_HINT}\n例如: /绑定 MyName"


def _case_variants(name):
    """生成指令名的大小写变体（中文字符不受影响）。"""
    if isinstance(name, tuple):
        return {name, tuple(s.lower() for s in name), tuple(s.upper() for s in name)}
    return {name, name.lower(), name.upper()}


def on_command(cmd, *, aliases=None, **kwargs):
    """大小写不敏感版的 on_command：为每个指令名/别名自动添加 lower/upper 变体。"""
    expanded: set = set()
    for n in [cmd, *(aliases or set())]:
        expanded |= _case_variants(n)
    expanded.discard(cmd)
    return _on_command(cmd, aliases=expanded or None, **kwargs)
