import traceback

from .common import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service

# Service definition
status_service = r5_service.create_subservice("status")

# Matchers
server_status = on_command(
    "状态", aliases={"服务器", "status", "server"}, priority=5, block=True
)


@server_status.handle()
@status_service.patch_handler()
async def handle_server_status(args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()

    # Optional server filter
    params = {}
    if content:
        params["server_name"] = content

    try:
        # 只查询已经 RCON 同步的中国服，并使用 simple 模式减少返回体积
        resp = await api_client.get_servers(
            server_name=params.get("server_name"),
            simple=True,
            cn_only=True,
            timeout=5.0,
        )

        if resp.status_code != 200:
            await server_status.finish(f"❌ 查询失败: HTTP {resp.status_code}")

        res = resp.json()
        if res.get("code") != "0000":
            await server_status.finish(f"❌ 查询失败: {res.get('msg')}")

        data = res.get("data", [])
        if not data:
            await server_status.finish("ℹ️ 当前没有服务器在线或没有匹配的服务器。")

        msg = "🖥️ 服务器状态列表\n"
        for s in data:
            name = s.get("short_name") or s.get("name", "Unknown")
            count = s.get("player_count", 0)
            max_players = s.get("max_players", 0)
            ping = s.get("ping", 0)
            msg += f"{name} 👥 在线: {count}/{max_players} | 📶 Ping: {ping}\n"

        await server_status.finish(msg.strip())

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await server_status.finish(f"❌ 查询出错: {e}")
