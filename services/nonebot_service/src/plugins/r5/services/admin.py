import traceback

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service

# Service definition
admin_service = r5_service.create_subservice("admin")
ban_service = admin_service.create_subservice("ban")
kick_service = admin_service.create_subservice("kick")
unban_service = admin_service.create_subservice("unban")

ALLOWED_REASONS = ["NO_COVER", "BE_POLITE", "CHEAT", "RULES"]

# Matchers
cmd_ban = on_command("ban", priority=5, block=True)
cmd_kick = on_command("kick", priority=5, block=True)
cmd_unban = on_command("unban", priority=5, block=True)


@cmd_ban.handle()
@ban_service.patch_handler()
async def handle_ban(args: Message = CommandArg()) -> None:
    text = args.extract_plain_text().strip()
    parts = text.split()

    if not parts:
        await cmd_ban.finish(f"⚠️ 用法: /ban <玩家名或ID> [原因]\n默认原因: NO_COVER\n可选原因: {', '.join(ALLOWED_REASONS)}")

    target = parts[0]
    reason = "NO_COVER"
    if len(parts) > 1:
        reason = parts[1].upper()

    if reason not in ALLOWED_REASONS:
        await cmd_ban.finish(f"❌ 原因不合法。\n可选原因: {', '.join(ALLOWED_REASONS)}")

    await cmd_ban.send(f"⏳ 正在执行封禁: {target}，请稍候...")

    try:
        resp = await api_client.ban_player(target, reason, timeout=5.0)
        res = resp.json()

        if res.get("code") == "0000":
            data = res.get("data") or {}
            if data.get("player_online") is True:
                primary = data.get("primary_server") or {}
                server_name = primary.get("name") or (
                    f"{primary.get('host', 'unknown')}:"
                    f"{primary.get('port', 'unknown')}"
                )
                async_count = data.get("async_server_count", 0)
                await cmd_ban.finish(
                    f"✅ 封禁成功\n"
                    f"玩家在线: 是\n"
                    f"已封禁服务器: {server_name}\n"
                    f"后台补执行服务器数: {async_count}\n"
                    f"{res.get('msg')}"
                )
            elif data.get("player_online") is False:
                async_count = data.get("async_server_count", 0)
                await cmd_ban.finish(
                    f"✅ 已启动后台封禁任务\n"
                    f"玩家在线: 否\n"
                    f"后台执行服务器数: {async_count}\n"
                    f"{res.get('msg')}"
                )
            else:
                await cmd_ban.finish(f"✅ {res.get('msg')}")
        else:
            await cmd_ban.finish(f"❌ 封禁失败: {res.get('msg')}")

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_ban.finish(f"❌ 执行出错: {e}")


@cmd_kick.handle()
@kick_service.patch_handler()
async def handle_kick(args: Message = CommandArg()) -> None:
    text = args.extract_plain_text().strip()
    parts = text.split()

    if not parts:
        await cmd_kick.finish(f"⚠️ 用法: /kick <玩家名或ID> [原因]\n默认原因: NO_COVER\n可选原因: {', '.join(ALLOWED_REASONS)}")

    target = parts[0]
    reason = "NO_COVER"
    if len(parts) > 1:
        reason = parts[1].upper()

    if reason not in ALLOWED_REASONS:
        await cmd_kick.finish(f"❌ 原因不合法。\n可选原因: {', '.join(ALLOWED_REASONS)}")

    try:
        resp = await api_client.kick_player(target, reason, timeout=5.0)
        res = resp.json()

        if res.get("code") == "0000":
            await cmd_kick.finish(f"✅ {res.get('msg')}")
        else:
            await cmd_kick.finish(f"❌ 踢出失败: {res.get('msg')}")

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_kick.finish(f"❌ 执行出错: {e}")


@cmd_unban.handle()
@unban_service.patch_handler()
async def handle_unban(args: Message = CommandArg()) -> None:
    target = args.extract_plain_text().strip()
    if not target:
        await cmd_unban.finish("⚠️ 用法: /unban <玩家名或ID>")

    await cmd_unban.send(f"⏳ 正在执行解封: {target}，请稍候...")

    try:
        resp = await api_client.unban_player(target, timeout=12.0)
        res = resp.json()

        if res.get("code") == "0000":
            await cmd_unban.finish(f"✅ {res.get('msg')}")
        else:
            await cmd_unban.finish(f"❌ 解封失败: {res.get('msg')}")

    except FinishedException:
        raise
    except httpx.ReadTimeout:
        traceback.print_exc()
        await cmd_unban.finish(
            "⏳ 解封请求超时，服务器可能仍在后台执行，请稍后重试或查询状态。"
        )
    except Exception as e:
        traceback.print_exc()
        await cmd_unban.finish(f"❌ 执行出错: {e}")
