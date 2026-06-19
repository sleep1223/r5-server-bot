import traceback

import httpx
from nonebot.adapters.onebot.v11 import Bot, Event, Message
from nonebot.exception import FinishedException
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from ..api_client import api_client
from .common import on_command, r5_service

# Service definition
admin_service = r5_service.create_subservice("admin")
ban_service = admin_service.create_subservice("ban")
kick_service = admin_service.create_subservice("kick")
unban_service = admin_service.create_subservice("unban")
alias_service = admin_service.create_subservice("alias")

ALLOWED_REASONS = ["NO_COVER", "BE_POLITE", "CHEAT", "RULES"]
REASON_CN = {
    "NO_COVER": "撤回掩体",
    "BE_POLITE": "言行不当",
    "CHEAT": "作弊",
    "RULES": "违反规则",
}
ERROR_CN = {
    "2001": "未找到该玩家",
    "2002": "该玩家无 Nucleus ID",
    "3002": "RCON 操作失败",
    "3003": "当前没有在线服务器",
}

# Matchers
cmd_ban = on_command("ban", priority=5, block=True)
cmd_kick = on_command("kick", priority=5, block=True)
cmd_unban = on_command("unban", priority=5, block=True)
cmd_set_alias = on_command("设置别名", aliases={"server_alias", "服务器别名"}, priority=5, block=True)
cmd_clear_alias = on_command("清除别名", aliases={"server_alias_clear"}, priority=5, block=True)


def _get_server_name(server: dict) -> str:
    """从服务器字典中提取可读名称"""
    return server.get("name") or f"{server.get('host', '?')}:{server.get('port', '?')}"


def _error_msg(res: dict) -> str:
    """将 API 错误码转为中文提示"""
    code = res.get("code", "")
    detail = res.get("detail")
    if detail:
        return str(detail)
    return ERROR_CN.get(code, res.get("msg") or "未知错误")


async def _is_superuser(bot: Bot, event: Event) -> bool:
    return await SUPERUSER(bot, event)


def _rcon_summary(data: dict) -> str:
    total = int(data.get("broadcast_total") or 0)
    success_count = int(data.get("broadcast_success_count") or 0)
    if data.get("rcon_skipped"):
        return "⚠️ RCON 未执行：当前没有可执行的在线服务器"
    if data.get("rcon_failed"):
        return f"⚠️ RCON 已广播 {total} 台服务器但未成功"
    return f"🔄 RCON 成功: {success_count}/{total}"


@cmd_ban.handle()
@ban_service.patch_handler()
async def handle_ban(bot: Bot, event: Event, args: Message = CommandArg()) -> None:
    if not await _is_superuser(bot, event):
        await cmd_ban.finish("⛔ NoneBot 端不再向普通管理员开放 /ban，请使用 /kick。")

    text = args.extract_plain_text().strip()
    parts = text.split()

    if not parts:
        await cmd_ban.finish(f"⚠️ 用法: /ban <玩家名或ID> [原因]\n\n默认原因: NO_COVER\n可选: {' | '.join(ALLOWED_REASONS)}")

    target = parts[0]
    reason = "NO_COVER"
    if len(parts) > 1:
        reason = parts[1].upper()

    if reason not in ALLOWED_REASONS:
        await cmd_ban.finish(f"❌ 原因不合法\n可选: {' | '.join(ALLOWED_REASONS)}")

    reason_cn = REASON_CN.get(reason, reason)
    await cmd_ban.send(f"⏳ 正在封禁 {target}...")

    try:
        resp = await api_client.ban_player(target, reason, timeout=5.0)
        res = resp.json()

        if res.get("code") != "0000":
            await cmd_ban.finish(f"❌ 封禁失败: {_error_msg(res)}")

        data = res.get("data") or {}
        player = data.get("player") or {}
        player_name = player.get("name") or target
        await cmd_ban.finish(f"🔨 封禁已提交\n\n👤 玩家: {player_name}\n📌 原因: {reason_cn}\n{_rcon_summary(data)}")

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
        await cmd_kick.finish(f"⚠️ 用法: /kick <玩家名或ID> [原因]\n\n默认原因: NO_COVER\n可选: {' | '.join(ALLOWED_REASONS)}")

    target = parts[0]
    reason = "NO_COVER"
    if len(parts) > 1:
        reason = parts[1].upper()

    if reason not in ALLOWED_REASONS:
        await cmd_kick.finish(f"❌ 原因不合法\n可选: {' | '.join(ALLOWED_REASONS)}")

    reason_cn = REASON_CN.get(reason, reason)

    try:
        resp = await api_client.kick_player(target, reason, timeout=5.0)
        res = resp.json()

        if res.get("code") != "0000":
            await cmd_kick.finish(f"❌ 踢出失败: {_error_msg(res)}")

        data = res.get("data") or {}
        player = data.get("player") or {}
        player_name = player.get("name") or target
        hit_server = data.get("hit_server") or {}
        server_line = f"\n🖥️ 服务器: {_get_server_name(hit_server)}" if hit_server else ""
        await cmd_kick.finish(f"👢 踢出已提交\n\n👤 玩家: {player_name}\n📌 原因: {reason_cn}{server_line}\n{_rcon_summary(data)}")

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_kick.finish(f"❌ 执行出错: {e}")


@cmd_unban.handle()
@unban_service.patch_handler()
async def handle_unban(bot: Bot, event: Event, args: Message = CommandArg()) -> None:
    if not await _is_superuser(bot, event):
        await cmd_unban.finish("⛔ NoneBot 端不再向普通管理员开放 /unban。")

    target = args.extract_plain_text().strip()
    if not target:
        await cmd_unban.finish("⚠️ 用法: /unban <玩家名或ID>")

    await cmd_unban.send(f"⏳ 正在解封 {target}...")

    try:
        resp = await api_client.unban_player(target, timeout=12.0)
        res = resp.json()

        if res.get("code") != "0000":
            await cmd_unban.finish(f"❌ 解封失败: {_error_msg(res)}")

        data = res.get("data") or {}
        player = data.get("player") or {}
        player_name = player.get("name") or target
        released_count = len(data.get("released_rules") or [])
        await cmd_unban.finish(f"🔓 解封已提交\n\n👤 玩家: {player_name}\n🧹 释放规则: {released_count}\n{_rcon_summary(data)}")

    except FinishedException:
        raise
    except httpx.ReadTimeout:
        traceback.print_exc()
        await cmd_unban.finish("⏳ 解封请求超时\n\n服务器可能仍在后台执行\n请稍后查询玩家状态确认")
    except Exception as e:
        traceback.print_exc()
        await cmd_unban.finish(f"❌ 执行出错: {e}")


@cmd_set_alias.handle()
@alias_service.patch_handler()
async def handle_set_alias(args: Message = CommandArg()) -> None:
    text = args.extract_plain_text().strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await cmd_set_alias.finish("⚠️ 用法: /设置别名 <服务器IP> <中文别名>")

    host, alias = parts[0].strip(), parts[1].strip()
    if not host or not alias:
        await cmd_set_alias.finish("⚠️ 用法: /设置别名 <服务器IP> <中文别名>")

    try:
        resp = await api_client.set_server_alias(host, alias, timeout=5.0)
        res = resp.json()
        if res.get("code") != "0000":
            await cmd_set_alias.finish(f"❌ 设置失败: {res.get('msg') or '未知错误'}")
        data = res.get("data") or {}
        await cmd_set_alias.finish(f"✅ 别名已设置\n🖥️ {data.get('host')} → {data.get('short_name')}")
    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_set_alias.finish(f"❌ 执行出错: {e}")


@cmd_clear_alias.handle()
@alias_service.patch_handler()
async def handle_clear_alias(args: Message = CommandArg()) -> None:
    host = args.extract_plain_text().strip()
    if not host:
        await cmd_clear_alias.finish("⚠️ 用法: /清除别名 <服务器IP>")

    try:
        resp = await api_client.set_server_alias(host, None, timeout=5.0)
        res = resp.json()
        if res.get("code") != "0000":
            await cmd_clear_alias.finish(f"❌ 清除失败: {res.get('msg') or '未知错误'}")
        await cmd_clear_alias.finish(f"✅ 别名已清空: {host}")
    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_clear_alias.finish(f"❌ 执行出错: {e}")
