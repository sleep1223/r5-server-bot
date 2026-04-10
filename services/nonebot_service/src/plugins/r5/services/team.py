import traceback

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, Message, PrivateMessageEvent
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service

team_svc = r5_service.create_subservice("team")

create_team_cmd = on_command("组队", priority=5, block=True)
list_teams_cmd = on_command("组队列表", aliases={"队伍列表"}, priority=4, block=True)
join_team_cmd = on_command("加入", aliases={"加入队伍"}, priority=5, block=True)
cancel_team_cmd = on_command("取消组队", priority=5, block=True)
leave_team_cmd = on_command("退出队伍", aliases={"退出组队"}, priority=5, block=True)
invite_cmd = on_command("邀请", priority=5, block=True)
accept_cmd = on_command("接受", priority=5, block=True)


async def _notify_full_team(bot: Bot, team_id: int, members: list[dict]) -> None:
    """队伍满员时，私信通知所有成员。"""
    member_lines = []
    for m in members:
        role_str = "队长" if m["role"] == "creator" else "队员"
        member_lines.append(f"  · [{role_str}] {m['player_name']} (QQ: {m['platform_uid']}) KD: {m.get('kd', '?')}")

    msg = f"🎮 队伍 #{team_id} 已满员！\n队友信息:\n" + "\n".join(member_lines)

    for m in members:
        if m["platform"] == "qq":
            try:
                await bot.send_private_msg(user_id=int(m["platform_uid"]), message=msg)
            except Exception:
                traceback.print_exc()


@create_team_cmd.handle()
@team_svc.patch_handler()
async def handle_create_team(event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    if not content or content not in ("1", "2"):
        await create_team_cmd.finish("⚠️ 请指定缺几个人，如: 组队 1 或 组队 2")

    slots_needed = int(content)
    user_id = event.get_user_id()

    try:
        resp = await api_client.create_team(platform="qq", platform_uid=user_id, slots_needed=slots_needed, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            await create_team_cmd.finish(f"❌ {req.get('msg', '创建失败')}")

        data = req.get("data", {})
        team_id = data.get("id", "?")
        msg = f"✅ 组队 #{team_id} 已发布！缺 {slots_needed} 人\n"
        msg += f"其他玩家可发送: 加入 {team_id}"
        await create_team_cmd.finish(msg)

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await create_team_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await create_team_cmd.finish(f"❌ 创建出错: {e}")


@list_teams_cmd.handle()
@team_svc.patch_handler()
async def handle_list_teams() -> None:
    try:
        resp = await api_client.list_teams(page_size=10, timeout=5.0)
        req = resp.json()
        data = req.get("data", [])

        if not data:
            await list_teams_cmd.finish("ℹ️ 当前没有开放的组队")

        msg = "📋 组队列表 (按KD排序)\n"
        msg += "ID | 队长 | KD | 缺人\n"
        msg += "-" * 30 + "\n"

        for t in data:
            creator = t.get("creator", {})
            msg += f"#{t['id']} {creator.get('player_name', '?')} "
            msg += f"KD:{creator.get('kd', '?')} "
            msg += f"缺{t.get('slots_remaining', '?')}人\n"

        msg += "\n发送 '加入 <队伍ID>' 加入队伍"
        await list_teams_cmd.finish(msg.strip())

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await list_teams_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await list_teams_cmd.finish(f"❌ 查询出错: {e}")


@join_team_cmd.handle()
@team_svc.patch_handler()
async def handle_join_team(bot: Bot, event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    if not content or not content.isdigit():
        await join_team_cmd.finish("⚠️ 请提供队伍ID，如: 加入 123")

    team_id = int(content)
    user_id = event.get_user_id()

    try:
        resp = await api_client.join_team(team_id=team_id, platform="qq", platform_uid=user_id, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            await join_team_cmd.finish(f"❌ {req.get('msg', '加入失败')}")

        data = req.get("data", {})
        notify_members = data.get("notify_members")

        if notify_members:
            await _notify_full_team(bot, team_id, notify_members)
            await join_team_cmd.finish(f"✅ 已加入队伍 #{team_id}，队伍已满员！已私信通知所有队友。")
        else:
            await join_team_cmd.finish(f"✅ 已加入队伍 #{team_id}，等待更多队友加入...")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await join_team_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await join_team_cmd.finish(f"❌ 加入出错: {e}")


@cancel_team_cmd.handle()
@team_svc.patch_handler()
async def handle_cancel_team(event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    if not content or not content.isdigit():
        await cancel_team_cmd.finish("⚠️ 请提供队伍ID，如: 取消组队 123")

    team_id = int(content)
    user_id = event.get_user_id()

    try:
        resp = await api_client.cancel_team(team_id=team_id, platform="qq", platform_uid=user_id, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            await cancel_team_cmd.finish(f"❌ {req.get('msg', '取消失败')}")

        await cancel_team_cmd.finish(f"✅ 队伍 #{team_id} 已取消")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await cancel_team_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await cancel_team_cmd.finish(f"❌ 取消出错: {e}")


@leave_team_cmd.handle()
@team_svc.patch_handler()
async def handle_leave_team(event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    if not content or not content.isdigit():
        await leave_team_cmd.finish("⚠️ 请提供队伍ID，如: 退出队伍 123")

    team_id = int(content)
    user_id = event.get_user_id()

    try:
        resp = await api_client.leave_team(team_id=team_id, platform="qq", platform_uid=user_id, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            await leave_team_cmd.finish(f"❌ {req.get('msg', '退出失败')}")

        await leave_team_cmd.finish(f"✅ 已退出队伍 #{team_id}")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await leave_team_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await leave_team_cmd.finish(f"❌ 退出出错: {e}")


@invite_cmd.handle()
@team_svc.patch_handler()
async def handle_invite(bot: Bot, event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    parts = content.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        await invite_cmd.finish("⚠️ 格式: 邀请 <队伍ID> <玩家昵称>")

    team_id = int(parts[0])
    target_name = parts[1].strip()
    user_id = event.get_user_id()

    try:
        resp = await api_client.invite_player(team_id=team_id, platform="qq", platform_uid=user_id, target_player_name=target_name, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            await invite_cmd.finish(f"❌ {req.get('msg', '邀请失败')}")

        data = req.get("data", {})
        target_uid = data.get("platform_uid")
        target_player = data.get("player_name", target_name)
        kd = data.get("kd", "?")

        # 私信通知被邀请的玩家
        if target_uid:
            invite_msg = f"🎮 玩家邀请你加入队伍 #{team_id}\n队长 KD: 查看组队列表获取\n回复: 接受 {team_id}"
            try:
                await bot.send_private_msg(user_id=int(target_uid), message=invite_msg)
            except Exception:
                traceback.print_exc()

        await invite_cmd.finish(f"✅ 已向 {target_player}(KD:{kd}) 发送邀请")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await invite_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await invite_cmd.finish(f"❌ 邀请出错: {e}")


@accept_cmd.handle()
@team_svc.patch_handler()
async def handle_accept(bot: Bot, event: Event, args: Message = CommandArg()) -> None:
    if not isinstance(event, PrivateMessageEvent):
        await accept_cmd.finish("⚠️ 请私信机器人接受邀请")

    content = args.extract_plain_text().strip()
    if not content or not content.isdigit():
        await accept_cmd.finish("⚠️ 请提供队伍ID，如: 接受 123")

    team_id = int(content)
    user_id = event.get_user_id()

    try:
        resp = await api_client.accept_invite(team_id=team_id, platform="qq", platform_uid=user_id, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            await accept_cmd.finish(f"❌ {req.get('msg', '接受失败')}")

        data = req.get("data", {})
        notify_members = data.get("notify_members")

        if notify_members:
            await _notify_full_team(bot, team_id, notify_members)
            await accept_cmd.finish(f"✅ 已加入队伍 #{team_id}，队伍已满员！已通知所有队友。")
        else:
            await accept_cmd.finish(f"✅ 已加入队伍 #{team_id}，等待更多队友...")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await accept_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await accept_cmd.finish(f"❌ 接受出错: {e}")
