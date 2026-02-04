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
        resp = await api_client.get_server_status(
            server_name=params.get("server_name"), timeout=5.0
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
            name = s.get("name", "Unknown")
            count = s.get("player_count", 0)
            ping = s.get("ping", 0)
            msg += f"[{name}] 👥 在线: {count} | 📶 Ping: {ping}\n"

        await server_status.finish(msg.strip())

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await server_status.finish(f"❌ 查询出错: {e}")
