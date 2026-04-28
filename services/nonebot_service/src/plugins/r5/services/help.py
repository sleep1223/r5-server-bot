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
        "🤖 R5 Bot 指令\n"
        "\n"
        "🔗 绑定 (需先添加机器人为好友)\n"
        "  📩 /绑定 <名字或NID> - 请私信机器人使用\n"
        "  🔓 /解绑\n"
        "  👤 /我的信息\n"
        "\n"
        "📊 数据统计\n"
        "  📈 /kd [范围] [排序]\n"
        "    范围: 今天|昨天|本周|本月\n"
        "    排序: 击杀|死亡|kd\n"
        "  👤 /个人kd [名字或NID]\n"
        "  🔫 /武器 - 武器排行榜\n"
        "  🎯 /个人武器 [名字或NID]\n"
        "\n"
        "🔍 查询\n"
        "  🖥️ /状态 - 服务器状态\n"
        "  👤 /查询玩家 <名字或NID>\n"
        "\n"
        "🎮 组队\n"
        "  👥 /组队 <1或2>\n"
        "  📋 /组队列表\n"
        "  ➕ /加入 <队伍ID>\n"
        "  ❌ /取消组队 <队伍ID>\n"
        "  🚪 /退出队伍 <队伍ID>\n"
        "  📣 /邀请 <队伍ID> <玩家昵称>\n"
        "  ✅ /接受 <队伍ID>\n"
        "\n"
        "💰 捐赠\n"
        "  📋 /捐赠查看\n"
        "\n"
        "🛠️ 管理\n"
        "  🚫 /ban <名字或NID> [原因]\n"
        "  👢 /kick <名字或NID> [原因]\n"
        "  🔓 /unban <名字或NID>\n"
        "  ➕ /捐赠新增 <名字> <金额> [备注]\n"
        "  🗑️ /捐赠删除 <序号>\n"
        "\n"
        "💡 先添加机器人好友，再私信 /绑定\n"
        "💡 绑定后可直接使用组队功能\n"
        "💡 绑定后 /个人kd 和 /个人武器 可不填写名字\n"
        "💡 支持模糊搜索玩家名\n"
        "🆔 NID = Nucleus ID"
    )


@cmd_help.handle()
@help_service.patch_handler()
async def handle_help(args: Message = CommandArg()) -> None:
    await cmd_help.finish(get_help_message())
