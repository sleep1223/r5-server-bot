from nonebot import get_plugin_config, require
from nonebot.plugin import PluginMetadata

from .config import Config

require("nonebot_plugin_access_control_api")


# Plugin Metadata
__plugin_meta__ = PluginMetadata(
    name="R5 Stats",
    description="R5 Reloaded Server Manager & Stats",
    usage="""
    指令:
    1. kd榜 [范围]
       范围: 今日/today, 昨日/yesterday, 本周/week, 本月/month, 全部/all (默认)
       示例: kd榜 今日

    2. 查kd <玩家名/ID>
       查询指定玩家对阵所有人的KD数据
       示例: 查kd 10086

    3. /状态 或 /服务器 [杭州/上海/广东]
       查询服务器状态

    4. /查询玩家 <玩家名/ID>
       查询玩家在线状态

    5. 管理指令:
       /ban <玩家名/ID>
       /kick <玩家名/ID>
       /unban <玩家名/ID>

    6. 捐赠指令:
       捐赠查看
       捐赠新增 <名字> <金额> [备注] (管理员)
       捐赠删除 <序号> (管理员)

    7. 个人武器 <玩家名/ID> [击杀/死亡/kd]
       查询指定玩家的武器击杀、死亡与KD
    """,
    config=Config,
)

from .services import admin, donation, help, kd, query, status, weapons

# Config
plugin_config = get_plugin_config(Config)

__all__ = ["admin", "donation", "help", "kd", "query", "status", "weapons"]
