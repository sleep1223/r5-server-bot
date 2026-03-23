import traceback

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service

# Service definition
status_service = r5_service.create_subservice("status")

# Matchers
server_status = on_command("状态", aliases={"服务器", "status", "server"}, priority=5, block=True)


@server_status.handle()
@status_service.patch_handler()
async def handle_server_status(args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()

    params = {}
    if content:
        params["server_name"] = content

    try:
        resp = await api_client.get_server_status(server_name=params.get("server_name"), timeout=5.0)

        if resp.status_code != 200:
            await server_status.finish(f"❌ HTTP {resp.status_code}")

        res = resp.json()
        if res.get("code") != "0000":
            await server_status.finish(f"❌ {res.get('msg')}")

        data = res.get("data", [])
        if not data:
            await server_status.finish("📡 当前无在线服务器")

        total_players = sum(s.get("player_count", 0) for s in data)

        msg = f"📡 服务器 {len(data)} 台 · 👥 {total_players} 人\n"
        msg += "━" * 20 + "\n"

        for s in data:
            name = s.get("short_name") or s.get("name", "未知")
            count = s.get("player_count", 0)
            max_players = s.get("max_players", 0)
            ping = s.get("ping", 0)
            country = s.get("country") or ""
            region = s.get("region") or ""
            location = f" · 🌍 {country} {region}".rstrip() if country else ""
            msg += f"🖥️ {name}\n"
            msg += f"  👥 {count}/{max_players} · 📶 {ping}ms{location}\n"

            players = s.get("players") or []
            if players:
                names = [p.get("name", "?") for p in players]
                msg += f"  🎮 {', '.join(names)}\n"

        await server_status.finish(msg.strip())

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await server_status.finish(f"❌ {e}")
