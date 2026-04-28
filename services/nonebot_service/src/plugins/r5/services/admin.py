import traceback

import httpx
from .common import on_command
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
    return ERROR_CN.get(code, res.get("msg") or "未知错误")


@cmd_ban.handle()
@ban_service.patch_handler()
async def handle_ban(args: Message = CommandArg()) -> None:
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
        async_count = data.get("async_server_count", 0)

        if data.get("skipped") and data.get("skip_reason") == "no_cover_allowed":
            primary = data.get("primary_server") or {}
            server_name = _get_server_name(primary)
            await cmd_ban.finish(f"⏭️ 已跳过封禁\n\n👤 玩家: {target}\n📌 原因: {reason_cn}\n🖥️ 服务器: {server_name}\n💡 该服务器允许撤回掩体，已跳过封禁")

        if data.get("player_online") is True:
            primary = data.get("primary_server") or {}
            server_name = _get_server_name(primary)
            await cmd_ban.finish(f"🔨 封禁成功\n\n👤 玩家: {target}\n📌 原因: {reason_cn}\n🟢 状态: 在线\n🖥️ 服务器: {server_name}\n🔄 后台同步: {async_count} 个服务器")
        elif data.get("player_online") is False:
            await cmd_ban.finish(f"🔨 已启动后台封禁\n\n👤 玩家: {target}\n📌 原因: {reason_cn}\n🔴 状态: 离线\n🔄 后台执行: {async_count} 个服务器")
        else:
            await cmd_ban.finish(f"🔨 封禁已提交: {target}")

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

        if data.get("skipped") and data.get("skip_reason") == "no_cover_allowed":
            server = data.get("server") or {}
            server_name = _get_server_name(server)
            await cmd_kick.finish(f"⏭️ 已跳过踢出\n\n👤 玩家: {target}\n📌 原因: {reason_cn}\n🖥️ 服务器: {server_name}\n💡 该服务器允许撤回掩体，已跳过踢出")

        if data.get("player_online") is True:
            server = data.get("server") or {}
            server_name = _get_server_name(server)
            await cmd_kick.finish(f"👢 踢出成功\n\n👤 玩家: {target}\n📌 原因: {reason_cn}\n🖥️ 服务器: {server_name}")
        elif data.get("player_online") is False:
            broadcast_total = data.get("broadcast_total", 0)
            fail_reason = data.get("fail_reason")
            if fail_reason == "no_online_servers":
                detail = "⚠️ 当前无在线服务器,RCON 踢出未执行"
            elif fail_reason == "no_server_hit":
                detail = f"⚠️ 已广播 {broadcast_total} 台服务器但未命中玩家\n可能玩家已离线,或玩家列表尚未刷新到该玩家"
            else:
                detail = "🔴 状态: 离线"
            await cmd_kick.finish(
                f"👢 踢出记录 +1\n\n👤 玩家: {target}\n📌 原因: {reason_cn}\n{detail}\n(已记录踢出次数)"
            )
        else:
            await cmd_kick.finish(f"👢 踢出已提交: {target}")

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

    await cmd_unban.send(f"⏳ 正在解封 {target}...")

    try:
        resp = await api_client.unban_player(target, timeout=12.0)
        res = resp.json()

        if res.get("code") != "0000":
            await cmd_unban.finish(f"❌ 解封失败: {_error_msg(res)}")

        data = res.get("data") or {}
        async_count = data.get("async_server_count", 0)
        target_server = data.get("target_server")
        target_source = data.get("target_source")

        if target_server:
            server_name = _get_server_name(target_server)
            source_cn = "封禁缓存" if target_source == "ban_cache" else "在线"
            await cmd_unban.finish(f"🔓 解封成功\n\n👤 玩家: {target}\n🖥️ 服务器: {server_name}\n📡 来源: {source_cn}\n🔄 后台同步: {async_count} 个服务器")
        else:
            await cmd_unban.finish(f"🔓 已启动后台解封\n\n👤 玩家: {target}\n🔴 状态: 离线\n🔄 后台执行: {async_count} 个服务器")

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
