from .common import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.params import CommandArg

from .common import r5_service

# Service definition
help_service = r5_service.create_subservice("help")

# Matchers
cmd_help = on_command("帮助", aliases={"help", "菜单", "menu"}, priority=5, block=True)


def get_help_message() -> str:
    return (
        "🤖 R5 Bot 指令（不区分大小写）\n"
        "\n"
        "🔗 绑定（私信机器人）\n"
        "  /绑定 <名字或NID> · /解绑 · /我的信息\n"
        "\n"
        "📊 统计\n"
        "  /kd [今天|昨天|本周|本月] [击杀|死亡|kd]\n"
        "  /武器 [范围]\n"
        "  /个人kd [范围] [排序]   默认本月，需绑定\n"
        "  /个人武器 [范围] [排序] 默认本月，需绑定\n"
        "\n"
        "🔍 查询\n"
        "  /状态 · /查询玩家 <名字或NID>\n"
        "\n"
        "👥 组队\n"
        "  /组队 <1或2> · /组队列表 · /加入 <ID>\n"
        "  /取消组队 <ID> · /退出队伍 <ID>\n"
        "  /邀请 <ID> <玩家> · /接受 <ID>\n"
        "\n"
        "💰 捐赠\n"
        "  /捐赠查看\n"
        "\n"
        "🛠️ 管理\n"
        "  /ban <名字或NID> [原因] · /kick · /unban\n"
        "  /捐赠新增 <名字> <金额> [备注] · /捐赠删除 <序号>\n"
        "\n"
        "💡 支持模糊搜索玩家名\n"
        "🆔 NID = Nucleus ID"
    )


@cmd_help.handle()
@help_service.patch_handler()
async def handle_help(args: Message = CommandArg()) -> None:
    await cmd_help.finish(get_help_message())
