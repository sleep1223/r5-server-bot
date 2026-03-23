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

REASON_CN = {
    "NO_COVER": "不遮挡",
    "BE_POLITE": "言行不当",
    "CHEAT": "作弊",
    "RULES": "违反规则",
}

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
        await cmd_ban.finish("❌ 用法：/ban <玩家> [原因]")

    target = parts[0]
    reason = "NO_COVER"
    if len(parts) > 1:
        reason = parts[1].upper()

    if reason not in ALLOWED_REASONS:
        await cmd_ban.finish(f"❌ 可选原因：{'｜'.join(ALLOWED_REASONS)}")

    reason_cn = REASON_CN.get(reason, reason)
    await cmd_ban.send(f"⏳ 封禁「{target}」（{reason_cn}）…")

    try:
        resp = await api_client.ban_player(target, reason, timeout=5.0)
        res = resp.json()

        if res.get("code") == "0000":
            data = res.get("data") or {}
            if data.get("player_online") is True:
                primary = data.get("primary_server") or {}
                server_name = primary.get("name") or (f"{primary.get('host', '未知')}:{primary.get('port', '未知')}")
                async_count = data.get("async_server_count", 0)
                msg = f"🚫 已封禁「{target}」\n🟢 在线 · 🖥️ {server_name}"
                if async_count > 0:
                    msg += f"\n🔄 同步剩余 {async_count} 台"
                await cmd_ban.finish(msg)
            elif data.get("player_online") is False:
                async_count = data.get("async_server_count", 0)
                await cmd_ban.finish(f"🚫 「{target}」离线\n🔄 后台封禁 {async_count} 台")
            else:
                await cmd_ban.finish(f"🚫 已封禁「{target}」")
        else:
            code = res.get("code", "")
            if code == "2001":
                await cmd_ban.finish(f"❌ 未找到「{target}」")
            elif code == "3001":
                await cmd_ban.finish("❌ RCON 配置缺失")
            elif code == "3002":
                await cmd_ban.finish(f"❌ 封禁「{target}」RCON 失败")
            else:
                await cmd_ban.finish(f"❌ {res.get('msg')}")

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_ban.finish(f"❌ {e}")


@cmd_kick.handle()
@kick_service.patch_handler()
async def handle_kick(args: Message = CommandArg()) -> None:
    text = args.extract_plain_text().strip()
    parts = text.split()

    if not parts:
        await cmd_kick.finish("❌ 用法：/kick <玩家> [原因]")

    target = parts[0]
    reason = "NO_COVER"
    if len(parts) > 1:
        reason = parts[1].upper()

    if reason not in ALLOWED_REASONS:
        await cmd_kick.finish(f"❌ 可选原因：{'｜'.join(ALLOWED_REASONS)}")

    reason_cn = REASON_CN.get(reason, reason)

    try:
        resp = await api_client.kick_player(target, reason, timeout=5.0)
        res = resp.json()

        if res.get("code") == "0000":
            data = res.get("data") or {}
            if data.get("player_online") is False:
                await cmd_kick.finish(f"⚠️ 「{target}」离线 · 已记录（{reason_cn}）")
            else:
                server = data.get("server") or {}
                server_name = server.get("name") or "未知"
                await cmd_kick.finish(f"⚠️ 已踢出「{target}」（{reason_cn}）\n🖥️ {server_name}")
        else:
            code = res.get("code", "")
            if code == "2001":
                await cmd_kick.finish(f"❌ 未找到「{target}」")
            elif code == "3002":
                await cmd_kick.finish(f"❌ 踢出「{target}」RCON 失败")
            else:
                await cmd_kick.finish(f"❌ {res.get('msg')}")

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_kick.finish(f"❌ {e}")


@cmd_unban.handle()
@unban_service.patch_handler()
async def handle_unban(args: Message = CommandArg()) -> None:
    target = args.extract_plain_text().strip()
    if not target:
        await cmd_unban.finish("❌ 用法：/unban <玩家>")

    await cmd_unban.send(f"⏳ 解封「{target}」…")

    try:
        resp = await api_client.unban_player(target, timeout=12.0)
        res = resp.json()

        if res.get("code") == "0000":
            data = res.get("data") or {}
            async_count = data.get("async_server_count", 0)
            target_server = data.get("target_server") or {}
            server_name = target_server.get("name") or ""
            if data.get("player_online"):
                msg = f"✅ 已解封「{target}」（在线）"
                if server_name:
                    msg += f"\n🖥️ {server_name}"
                await cmd_unban.finish(msg)
            elif async_count > 0:
                msg = f"✅ 「{target}」离线\n🔄 后台解封 {async_count} 台"
                if server_name:
                    msg += f"\n🖥️ 首台：{server_name}"
                await cmd_unban.finish(msg)
            else:
                await cmd_unban.finish(f"✅ 已解封「{target}」")
        else:
            code = res.get("code", "")
            if code == "2001":
                await cmd_unban.finish(f"❌ 未找到「{target}」")
            elif code == "3003":
                await cmd_unban.finish("❌ 无在线服务器")
            elif code == "3002":
                await cmd_unban.finish(f"❌ 解封「{target}」RCON 失败")
            else:
                await cmd_unban.finish(f"❌ {res.get('msg')}")

    except FinishedException:
        raise
    except httpx.ReadTimeout:
        traceback.print_exc()
        await cmd_unban.finish("⏱️ 超时，服务器可能仍在执行\n稍后 /查询 确认")
    except Exception as e:
        traceback.print_exc()
        await cmd_unban.finish(f"❌ {e}")
