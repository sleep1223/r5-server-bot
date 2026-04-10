import traceback

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service

# Service definition
query_service = r5_service.create_subservice("query")

# Matchers
player_query = on_command("查询玩家", aliases={"查询", "query"}, priority=5, block=True)


@player_query.handle()
@query_service.patch_handler()
async def handle_player_query(args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    if not content:
        await player_query.finish("⚠️ 请提供玩家名或ID，如: /查询 MyName")

    try:
        resp = await api_client.query_player(content, page_no=1, page_size=20, timeout=5.0)

        if resp.status_code != 200:
            await player_query.finish(f"❌ 查询失败: HTTP {resp.status_code}")

        res = resp.json()
        if res.get("code") == "4001":  # Not found
            await player_query.finish(f"❌ 未找到玩家: {content}")

        if res.get("code") != "0000":
            await player_query.finish(f"❌ 查询失败: {res.get('msg')}")

        data = res.get("data", [])
        if not data:
            await player_query.finish(f"❌ 未找到玩家: {content}")

        # 优先显示在线玩家，只显示前3个
        data.sort(key=lambda x: x.get("is_online", False), reverse=True)
        data = data[:3]

        msg = f"🔍 查询: {content}\n"
        for i, item in enumerate(data):
            p = item.get("player", {})
            server = item.get("server")
            is_online = item.get("is_online")
            ping = item.get("ping", 0)

            status_str = p.get("status", "unknown")
            ban_count = p.get("ban_count", 0)
            kick_count = p.get("kick_count", 0)

            status_map = {
                "online": ("🟢", "在线"),
                "offline": ("🔴", "离线"),
                "banned": ("🚫", "封禁"),
                "kicked": ("⚠️", "踢出"),
            }
            status_icon, status_text = status_map.get(status_str, ("❓", status_str))

            if status_icon == "❓":
                status_icon = "🟢" if is_online else "🔴"

            if i > 0:
                msg += "\n"
            msg += f"\n{status_icon} {p.get('name')}\n"
            msg += f"🆔 {p.get('nucleus_id')}\n"
            msg += f"📋 {status_text}\n"
            if ban_count or kick_count:
                msg += f"🚫 封禁 {ban_count} 次 / ⚠️ 踢出 {kick_count} 次\n"
            country = p.get("country") or "未知"
            region = p.get("region") or "未知"
            msg += f"🌍 {country} / {region}\n"

            total_playtime = item.get("total_playtime_seconds", 0)
            if total_playtime >= 3600:
                hours = total_playtime // 3600
                minutes = (total_playtime % 3600) // 60
                msg += f"🕐 总游玩 {hours} 小时 {minutes} 分钟\n"
            elif total_playtime >= 60:
                msg += f"🕐 总游玩 {total_playtime // 60} 分钟\n"

            if is_online:
                msg += f"📶 {ping}ms\n"
                if server:
                    server_name = server.get("short_name") or server.get("name")
                    msg += f"🖥️ {server_name}\n"
                duration = item.get("duration_seconds", 0)
                msg += f"⏱️ 本次在线 {duration // 60} 分钟\n"
            elif status_str == "banned" and server:
                server_name = server.get("short_name") or server.get("name")
                source = "(缓存)" if item.get("server_source") == "ban_cache" else ""
                msg += f"🖥️ 封禁服务器{source}: {server_name}\n"

        await player_query.finish(msg.strip())

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await player_query.finish(f"❌ 查询出错: {e}")
