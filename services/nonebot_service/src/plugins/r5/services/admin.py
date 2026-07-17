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

ALLOWED_REASONS = ["NO_COVER", "BE_POLITE", "CHEAT", "RULES", "NO_SPAM_CROUCH"]
REASON_CN = {
    "NO_COVER": "撤回掩体",
    "BE_POLITE": "言行不当",
    "CHEAT": "作弊",
    "RULES": "违反规则",
    "NO_SPAM_CROUCH": "滥用AD蹲动画",
}
ERROR_CN = {
    "2001": "未找到该玩家",
    "2002": "该玩家无 Nucleus ID",
    "3003": "当前没有在线服务器",
}

# Matchers
cmd_ban = on_command("ban", priority=5, block=True)
cmd_kick = on_command("kick", priority=5, block=True)
cmd_unban = on_command("unban", priority=5, block=True)
cmd_set_alias = on_command("设置别名", aliases={"server_alias", "服务器别名"}, priority=5, block=True)
cmd_clear_alias = on_command("清除别名", aliases={"server_alias_clear"}, priority=5, block=True)


def _error_msg(res: dict) -> str:
    """将 API 错误码转为中文提示"""
    code = res.get("code", "")
    detail = res.get("detail")
    if detail:
        return str(detail)
    return ERROR_CN.get(code, res.get("msg") or "未知错误")


def _auth_failure_message(resp: httpx.Response, res: dict, *, action_label: str, endpoint: str, operator_qq: str, target: str, reason: str | None = None) -> str:
    message = _error_msg(res)
    if resp.status_code not in {401, 403}:
        return f"❌ {action_label}失败: {message}"

    reason_line = f"原因: {reason}\n" if reason else ""
    return (
        f"❌ {action_label}失败: {message}\n\n"
        "🔎 请求信息\n"
        f"接口: POST {api_client.base_url}{endpoint}\n"
        f"HTTP: {resp.status_code}\n"
        f"操作者QQ: {operator_qq}\n"
        f"目标: {target}\n"
        f"{reason_line}"
        f"返回 code: {res.get('code') or '-'}\n"
        f"返回 msg: {res.get('msg') or res.get('detail') or '-'}\n\n"
        "请确认 r5_api_token 配置正确，并确认该 QQ 已绑定玩家且已同步管理员权限。"
    )


async def _is_superuser(bot: Bot, event: Event) -> bool:
    return await SUPERUSER(bot, event)


def _player_display(player: dict, fallback: str) -> str:
    name = player.get("name") or fallback
    uid = player.get("nucleus_id")
    return f"{name} ({uid})" if uid else name


def _kick_notice_summary(data: dict) -> str:
    notice = data.get("notice") or {}
    if not notice:
        return ""

    context = notice.get("message_context") or {}
    reused = bool(context.get("pending_notice_reused"))
    status = "已复用待确认记录" if reused else "已创建待确认记录"
    return f"\n📩 Kick 确认: {status} #{notice.get('id')}\n🔗 自助解除: https://r5.sleep0.de/bans"


def _access_submit_summary(data: dict, action_label: str) -> str:
    execution_mode = data.get("execution_mode") or "sdk_access"
    if execution_mode == "sdk_access":
        return f"🧭 执行方式: SDK 准入上报生效，已写入{action_label}规则"
    return f"🧭 执行方式: {execution_mode}"


@cmd_ban.handle()
@ban_service.patch_handler()
async def handle_ban(event: Event, args: Message = CommandArg()) -> None:
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
        operator_qq = event.get_user_id()
        resp = await api_client.ban_player(operator_qq, target, reason, timeout=5.0)
        res = resp.json()

        if res.get("code") != "0000":
            await cmd_ban.finish(_auth_failure_message(resp, res, action_label="封禁", endpoint="/admin/bot/access-actions/ban", operator_qq=operator_qq, target=target, reason=reason))

        data = res.get("data") or {}
        player = data.get("player") or {}
        await cmd_ban.finish(f"🔨 封禁已提交\n\n👤 玩家: {_player_display(player, target)}\n📌 原因: {reason_cn}\n{_access_submit_summary(data, '封禁')}")

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_ban.finish(f"❌ 执行出错: {e}")


@cmd_kick.handle()
@kick_service.patch_handler()
async def handle_kick(event: Event, args: Message = CommandArg()) -> None:
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
        operator_qq = event.get_user_id()
        resp = await api_client.kick_player(operator_qq, target, reason, timeout=5.0)
        res = resp.json()

        if res.get("code") != "0000":
            await cmd_kick.finish(_auth_failure_message(resp, res, action_label="踢出", endpoint="/admin/bot/access-actions/kick", operator_qq=operator_qq, target=target, reason=reason))

        data = res.get("data") or {}
        player = data.get("player") or {}
        operation = data.get("operation") or {}
        actual_action = str(operation.get("action") or "").lower()
        if data.get("action_escalated") or actual_action == "ban":
            ip_line = "\n🌐 IP 封禁: 未同步" if data.get("sync_player_ip") is False else ""
            await cmd_kick.finish(f"🔨 二次 Kick 已升级为封禁\n\n👤 玩家: {_player_display(player, target)}\n📌 原因: {reason_cn}{ip_line}\n{_access_submit_summary(data, '封禁')}")
        await cmd_kick.finish(f"✅ 踢出已提交\n\n👤 玩家: {_player_display(player, target)}\n📌 原因: {reason_cn}{_kick_notice_summary(data)}\n{_access_submit_summary(data, '踢出')}")

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
        operator_qq = event.get_user_id()
        resp = await api_client.unban_player(operator_qq, target, timeout=12.0)
        res = resp.json()

        if res.get("code") != "0000":
            await cmd_unban.finish(_auth_failure_message(resp, res, action_label="解封", endpoint="/admin/bot/access-actions/unban", operator_qq=operator_qq, target=target))

        data = res.get("data") or {}
        player = data.get("player") or {}
        released_count = len(data.get("released_rules") or [])
        await cmd_unban.finish(f"🔓 解封已提交\n\n👤 玩家: {_player_display(player, target)}\n🧹 释放规则: {released_count}\n🧭 执行方式: SDK 准入规则已释放")

    except FinishedException:
        raise
    except httpx.ReadTimeout:
        traceback.print_exc()
        await cmd_unban.finish("⏳ 解封请求超时\n\n请稍后查询玩家状态确认")
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
