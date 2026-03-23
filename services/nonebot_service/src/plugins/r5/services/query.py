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

STATUS_MAP = {
    "online": ("🟢", "在线"),
    "offline": ("🔴", "离线"),
    "banned": ("🚫", "已封禁"),
    "kicked": ("⚠️", "已踢出"),
}


@player_query.handle()
@query_service.patch_handler()
async def handle_player_query(args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    if not content:
        await player_query.finish("❌ 用法：/查询 <玩家名或ID>")

    try:
        resp = await api_client.query_player(content, page_no=1, page_size=20, timeout=5.0)

        if resp.status_code != 200:
            await player_query.finish(f"❌ HTTP {resp.status_code}")

        res = resp.json()
        if res.get("code") == "2001":
            await player_query.finish(f"❌ 未找到「{content}」")

        if res.get("code") != "0000":
            await player_query.finish(f"❌ {res.get('msg')}")

        data = res.get("data", [])
        if not data:
            await player_query.finish(f"❌ 未找到「{content}」")

        data.sort(key=lambda x: x.get("is_online", False), reverse=True)
        data = data[:3]

        msg = f"👤 查询：{content}\n"
        msg += "━" * 20 + "\n"

        for item in data:
            p = item.get("player", {})
            server = item.get("server")
            is_online = item.get("is_online")
            ping = item.get("ping", 0)

            status_str = p.get("status", "unknown")
            ban_count = p.get("ban_count", 0)
            kick_count = p.get("kick_count", 0)

            status_icon, status_text = STATUS_MAP.get(status_str, ("❓", status_str))

            if status_icon == "❓":
                status_icon = "🟢" if is_online else "🔴"

            msg += f"{status_icon} {p.get('name')}（{p.get('nucleus_id')}）\n"

            tags = [status_text]
            if ban_count > 0:
                tags.append(f"🚫×{ban_count}")
            if kick_count > 0:
                tags.append(f"⚠️×{kick_count}")
            msg += f"  {'｜'.join(tags)}\n"

            country = p.get("country") or "未知"
            region = p.get("region") or "未知"
            msg += f"  🌍 {country} {region}\n"

            if is_online:
                msg += f"  📶 {ping}ms"
                if server:
                    server_name = server.get("short_name") or server.get("name")
                    msg += f" · 🖥️ {server_name}"
                msg += "\n"
                duration = item.get("duration_seconds", 0)
                hours, remainder = divmod(duration, 3600)
                minutes = remainder // 60
                if hours > 0:
                    msg += f"  ⏱️ {hours}h{minutes}m\n"
                else:
                    msg += f"  ⏱️ {minutes}m\n"
            elif status_str == "banned" and server:
                server_name = server.get("short_name") or server.get("name")
                cache_tag = "（缓存）" if item.get("server_source") == "ban_cache" else ""
                msg += f"  🚫 {server_name}{cache_tag}\n"

            msg += "━" * 20 + "\n"

        await player_query.finish(msg.strip())

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await player_query.finish(f"❌ {e}")
