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
    msg = """🤖 R5 Bot 帮助菜单 🤖

📊 数据统计 (Stats):
• /kd [范围] [排序]- 查看KD排行榜
  范围: 今天 (默认), 昨天, 本周, 本月, 全部
  排序: 击杀, 死亡, kd
• /个人kd <NID/名字> - 查询玩家击杀数据
• /武器 - 查看武器排行榜
• /个人武器 <NID/名字> - 查询玩家武器数据

💰 捐赠系统 (Donation):
• /捐赠查看 - 查看捐赠列表

🔍 查询与状态 (Query):
• /状态 - 查看服务器运行状态
• /查询玩家 <NID/名字> - 查询玩家在线状态

🛠️ 管理指令 (Admin):
• /ban <NID/名字> [原因] - 封禁玩家 (默认: NO_COVER)
• /kick <NID/名字> [原因] - 踢出玩家 (默认: NO_COVER)
• /unban <NID/名字> - 解封玩家
• /捐赠新增 <名字> <金额> [备注]
• /捐赠删除 <序号>

💡 提示:
• 指令均支持模糊搜索玩家名字
• NID 指的是 Nucleus ID
"""
    await cmd_help.finish(msg)
