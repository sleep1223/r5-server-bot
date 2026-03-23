from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.params import CommandArg

from .common import r5_service

# Service definition
help_service = r5_service.create_subservice("help")

# Matchers
cmd_help = on_command("帮助", aliases={"help", "菜单", "menu"}, priority=5, block=True)


@cmd_help.handle()
@help_service.patch_handler()
async def handle_help(args: Message = CommandArg()) -> None:
    msg = (
        "📋 R5 Bot 菜单\n"
        "━━━━━━━━━━━━━━━━\n"
        "\n"
        "🔍 查询\n"
        "  📡 /状态 — 服务器状态\n"
        "  👤 /查询 <玩家> — 在线状态\n"
        "\n"
        "📊 数据\n"
        "  🏆 /kd [范围] [排序] — KD 榜\n"
        "  📈 /查kd <玩家> — 对战数据\n"
        "  🔫 /武器 [范围] [排序] — 武器榜\n"
        "  🎯 /个人武器 <玩家> — 武器数据\n"
        "\n"
        "🛡️ 管理\n"
        "  🚫 /ban <玩家> [原因]\n"
        "  ⚠️ /kick <玩家> [原因]\n"
        "  ✅ /unban <玩家>\n"
        "\n"
        "💰 捐赠\n"
        "  📜 /捐赠查看\n"
        "  ➕ /捐赠新增 <名字> <金额> [备注]\n"
        "  🗑️ /捐赠删除 <序号>\n"
        "\n"
        "━━━━━━━━━━━━━━━━\n"
        "💡 <玩家> 支持名字/ID 模糊搜索\n"
        "📅 范围：今天｜昨天｜本周｜本月｜全部\n"
        "🔗 r5.sleep0.de"
    )
    await cmd_help.finish(msg)
