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
        reasons = "、".join(ALLOWED_REASONS)
        await cmd_ban.finish(f"用法：/ban <玩家名或ID> [原因]\n默认原因：NO_COVER\n可选原因：{reasons}")

    target = parts[0]
    reason = "NO_COVER"
    if len(parts) > 1:
        reason = parts[1].upper()

    if reason not in ALLOWED_REASONS:
        await cmd_ban.finish(f"原因不合法\n可选：{'、'.join(ALLOWED_REASONS)}")

    reason_cn = REASON_CN.get(reason, reason)
    await cmd_ban.send(f"正在封禁「{target}」（{reason_cn}），请稍候…")

    try:
        resp = await api_client.ban_player(target, reason, timeout=5.0)
        res = resp.json()

        if res.get("code") == "0000":
            data = res.get("data") or {}
            if data.get("player_online") is True:
                primary = data.get("primary_server") or {}
                server_name = primary.get("name") or (f"{primary.get('host', '未知')}:{primary.get('port', '未知')}")
                async_count = data.get("async_server_count", 0)
                msg = f"封禁成功\n玩家「{target}」正在线\n已封禁服务器：{server_name}\n"
                if async_count > 0:
                    msg += f"后台同步中（剩余 {async_count} 台服务器）"
                await cmd_ban.finish(msg.strip())
            elif data.get("player_online") is False:
                async_count = data.get("async_server_count", 0)
                await cmd_ban.finish(f"玩家「{target}」当前不在线\n已启动后台封禁（共 {async_count} 台服务器）")
            else:
                await cmd_ban.finish(f"封禁完成：{target}")
        else:
            code = res.get("code", "")
            if code == "2001":
                await cmd_ban.finish(f"未找到玩家「{target}」")
            elif code == "3001":
                await cmd_ban.finish("RCON 配置缺失，无法执行封禁")
            elif code == "3002":
                await cmd_ban.finish(f"封禁「{target}」失败，RCON 操作未成功")
            else:
                await cmd_ban.finish(f"封禁失败: {res.get('msg')}")

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_ban.finish(f"执行出错: {e}")


@cmd_kick.handle()
@kick_service.patch_handler()
async def handle_kick(args: Message = CommandArg()) -> None:
    text = args.extract_plain_text().strip()
    parts = text.split()

    if not parts:
        reasons = "、".join(ALLOWED_REASONS)
        await cmd_kick.finish(f"用法：/kick <玩家名或ID> [原因]\n默认原因：NO_COVER\n可选原因：{reasons}")

    target = parts[0]
    reason = "NO_COVER"
    if len(parts) > 1:
        reason = parts[1].upper()

    if reason not in ALLOWED_REASONS:
        await cmd_kick.finish(f"原因不合法\n可选：{'、'.join(ALLOWED_REASONS)}")

    reason_cn = REASON_CN.get(reason, reason)

    try:
        resp = await api_client.kick_player(target, reason, timeout=5.0)
        res = resp.json()

        if res.get("code") == "0000":
            data = res.get("data") or {}
            if data.get("player_online") is False:
                await cmd_kick.finish(f"玩家「{target}」当前不在线，已记录踢出（{reason_cn}）")
            else:
                await cmd_kick.finish(f"已踢出玩家「{target}」（{reason_cn}）")
        else:
            code = res.get("code", "")
            if code == "2001":
                await cmd_kick.finish(f"未找到玩家「{target}」")
            elif code == "3002":
                await cmd_kick.finish(f"踢出「{target}」失败，RCON 操作未成功")
            else:
                await cmd_kick.finish(f"踢出失败: {res.get('msg')}")

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_kick.finish(f"执行出错: {e}")


@cmd_unban.handle()
@unban_service.patch_handler()
async def handle_unban(args: Message = CommandArg()) -> None:
    target = args.extract_plain_text().strip()
    if not target:
        await cmd_unban.finish("用法：/unban <玩家名或ID>")

    await cmd_unban.send(f"正在解封「{target}」，请稍候…")

    try:
        resp = await api_client.unban_player(target, timeout=12.0)
        res = resp.json()

        if res.get("code") == "0000":
            data = res.get("data") or {}
            async_count = data.get("async_server_count", 0)
            if data.get("player_online"):
                await cmd_unban.finish(f"已解封在线玩家「{target}」")
            elif async_count > 0:
                await cmd_unban.finish(f"玩家「{target}」当前不在线\n已启动后台解封（共 {async_count} 台服务器）")
            else:
                await cmd_unban.finish(f"已解封玩家「{target}」")
        else:
            code = res.get("code", "")
            if code == "2001":
                await cmd_unban.finish(f"未找到玩家「{target}」")
            elif code == "3003":
                await cmd_unban.finish("当前没有在线服务器，无法执行解封")
            elif code == "3002":
                await cmd_unban.finish(f"解封「{target}」失败，RCON 操作未成功")
            else:
                await cmd_unban.finish(f"解封失败: {res.get('msg')}")

    except FinishedException:
        raise
    except httpx.ReadTimeout:
        traceback.print_exc()
        await cmd_unban.finish("解封请求超时，服务器可能仍在后台执行\n请稍后查询玩家状态确认结果")
    except Exception as e:
        traceback.print_exc()
        await cmd_unban.finish(f"执行出错: {e}")
