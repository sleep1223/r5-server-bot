from nonebot import get_plugin_config, require
from nonebot.plugin import PluginMetadata

from .config import Config

require("nonebot_plugin_access_control_api")


# Plugin Metadata
__plugin_meta__ = PluginMetadata(
    name="R5 Stats",
    description="R5 Reloaded Server Manager & Stats",
    usage="""
    🔍 查询与状态:
       /状态  查看服务器运行状态
       /查询 <玩家名/ID>  查询玩家在线状态

    📊 数据统计:
       /kd [范围] [排序]  KD 排行榜
       /查kd <玩家名/ID>  玩家对战数据
       /武器  武器排行榜
       /个人武器 <玩家名/ID>  玩家武器数据

    🛡️ 管理指令:
       /ban <玩家名/ID> [原因]  封禁玩家
       /kick <玩家名/ID> [原因]  踢出玩家
       /unban <玩家名/ID>  解封玩家

    💰 捐赠系统:
       /捐赠查看  查看捐赠列表
       /捐赠新增 <名字> <金额> [备注] (管理员)
       /捐赠删除 <序号> (管理员)
    """,
    config=Config,
)

from .services import admin, donation, help, kd, query, status, weapons

# Config
plugin_config = get_plugin_config(Config)

__all__ = ["admin", "donation", "help", "kd", "query", "status", "weapons"]
