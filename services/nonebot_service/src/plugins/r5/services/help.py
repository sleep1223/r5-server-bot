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
        "R5 Bot 指令菜单\n"
        "━━━━━━━━━━━━━━━━\n"
        "\n"
        "【数据统计】\n"
        "  /kd [范围] [排序]\n"
        "    查看 KD 排行榜\n"
        "    范围：今天（默认）、昨天、本周、本月、全部\n"
        "    排序：击杀、死亡、kd\n"
        "  /查kd <玩家名或ID>\n"
        "    查询玩家对战数据\n"
        "  /武器\n"
        "    查看武器排行榜\n"
        "  /个人武器 <玩家名或ID>\n"
        "    查询玩家武器数据\n"
        "\n"
        "【查询与状态】\n"
        "  /状态\n"
        "    查看服务器运行状态\n"
        "  /查询 <玩家名或ID>\n"
        "    查询玩家在线状态\n"
        "\n"
        "【管理指令】\n"
        "  /ban <玩家名或ID> [原因]\n"
        "    封禁玩家（默认：NO_COVER）\n"
        "  /kick <玩家名或ID> [原因]\n"
        "    踢出玩家（默认：NO_COVER）\n"
        "  /unban <玩家名或ID>\n"
        "    解封玩家\n"
        "\n"
        "【捐赠系统】\n"
        "  /捐赠查看  查看捐赠列表\n"
        "  /捐赠新增 <名字> <金额> [备注]\n"
        "  /捐赠删除 <序号>\n"
        "\n"
        "━━━━━━━━━━━━━━━━\n"
        "提示：指令均支持模糊搜索玩家名字\n"
        "NID 指 Nucleus ID"
    )
    await cmd_help.finish(msg)
