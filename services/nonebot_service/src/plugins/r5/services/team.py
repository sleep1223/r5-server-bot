import json
import logging
import traceback
from pathlib import Path

import httpx
from .common import BINDING_GUIDE, on_command
from nonebot.adapters.onebot.v11 import Event, Message, MessageSegment
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service

team_svc = r5_service.create_subservice("team")

def _maybe_binding_hint(msg: str) -> str:
    """如果后端返回的是绑定相关错误，替换为带引导的提示。"""
    if "绑定" in msg:
        return BINDING_GUIDE
    return f"❌ {msg}"

TEAM_LOG_PATH = Path(__file__).resolve().parents[4] / "logs" / "team.log"


def _build_team_logger() -> logging.Logger:
    logger = logging.getLogger("r5.team")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    TEAM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(TEAM_LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


team_logger = _build_team_logger()


def _member_snapshot(member: dict) -> dict:
    return {
        "platform": member.get("platform"),
        "platform_uid": member.get("platform_uid"),
        "player_name": member.get("player_name"),
        "kd": member.get("kd"),
        "role": member.get("role"),
    }


def _team_snapshot(team_data: dict | None) -> dict:
    if not team_data:
        return {}

    creator = team_data.get("creator") or {}
    members = team_data.get("members") or []
    return {
        "id": team_data.get("id"),
        "status": team_data.get("status"),
        "slots_needed": team_data.get("slots_needed"),
        "slots_remaining": team_data.get("slots_remaining"),
        "creator": {
            "player_name": creator.get("player_name"),
            "platform_uid": creator.get("platform_uid"),
            "kd": creator.get("kd"),
        },
        "members": [_member_snapshot(member) for member in members],
    }


def _log_team_event(event: str, **payload) -> None:
    team_logger.info("%s | %s", event, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _format_member_line(member: dict) -> str:
    role_str = "队长" if member.get("role") == "creator" else "队员"
    return f"  · [{role_str}] {member.get('player_name', '未知')} (QQ: {member.get('platform_uid', '?')}) KD: {member.get('kd', '?')}"


def _format_team_overview(team_id: int, team_info: dict, title: str) -> str:
    members = team_info.get("members") or []
    current_size = len(members)
    slots_needed = team_info.get("slots_needed") or 0
    max_size = slots_needed + 1
    slots_remaining = team_info.get("slots_remaining")
    if slots_remaining is None:
        slots_remaining = max(max_size - current_size, 0)

    lines = [
        f"{title}",
        f"队伍ID: #{team_id}",
        f"当前人数: {current_size}/{max_size}",
        f"还缺人数: {slots_remaining}",
        "队伍成员:",
    ]
    lines.extend(_format_member_line(member) for member in members)
    return "\n".join(lines)


def _find_creator_member(members: list[dict]) -> dict | None:
    for member in members:
        if member.get("role") == "creator":
            return member
    return None


def _find_member_by_uid(members: list[dict], platform_uid: str) -> dict | None:
    for member in members:
        if str(member.get("platform_uid")) == str(platform_uid):
            return member
    return None


def _build_at_members(members: list[dict]) -> Message:
    """构建 @所有QQ成员 的消息段。"""
    msg = Message()
    for member in members:
        if member.get("platform") == "qq":
            msg += MessageSegment.at(int(member["platform_uid"])) + " "
    return msg


create_team_cmd = on_command("组队", priority=5, block=True)
list_teams_cmd = on_command("组队列表", aliases={"队伍列表"}, priority=4, block=True)
join_team_cmd = on_command("加入", aliases={"加入队伍"}, priority=5, block=True)
cancel_team_cmd = on_command("取消组队", priority=5, block=True)
leave_team_cmd = on_command("退出队伍", aliases={"退出组队"}, priority=5, block=True)
invite_cmd = on_command("邀请", priority=5, block=True)
accept_cmd = on_command("接受", priority=5, block=True)


@create_team_cmd.handle()
@team_svc.patch_handler()
async def handle_create_team(event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    if not content or content not in ("1", "2"):
        await create_team_cmd.finish("⚠️ 请指定缺几个人，如: /组队 1 或 /组队 2")

    slots_needed = int(content)
    user_id = event.get_user_id()

    try:
        resp = await api_client.create_team(platform="qq", platform_uid=user_id, slots_needed=slots_needed, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            _log_team_event(
                "team_create_rejected",
                user_id=user_id,
                slots_needed=slots_needed,
                code=req.get("code"),
                msg=req.get("msg", "创建失败"),
            )
            await create_team_cmd.finish(_maybe_binding_hint(req.get("msg", "创建失败")))

        data = req.get("data", {})
        team_id = data.get("id", "?")
        _log_team_event(
            "team_created",
            team_id=team_id,
            user_id=user_id,
            slots_needed=slots_needed,
            team=_team_snapshot(data),
        )
        msg = f"✅ 组队 #{team_id} 已发布！缺 {slots_needed} 人\n"
        msg += f"其他玩家可发送: /加入 {team_id}"
        await create_team_cmd.finish(msg)

    except FinishedException:
        raise
    except httpx.RequestError as e:
        _log_team_event("team_create_request_error", user_id=user_id, slots_needed=slots_needed, error=str(e))
        await create_team_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        team_logger.exception("team_create_failed | user_id=%s | slots_needed=%s", user_id, slots_needed)
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

        msg += "\n发送 '/加入 <队伍ID>' 加入队伍"
        msg += "\n🌐 也可以使用网页版: https://r5.sleep0.de/teams"
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
async def handle_join_team(event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    if not content or not content.isdigit():
        await join_team_cmd.finish("⚠️ 请提供队伍ID，如: /加入 123")

    team_id = int(content)
    user_id = event.get_user_id()

    try:
        resp = await api_client.join_team(team_id=team_id, platform="qq", platform_uid=user_id, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            _log_team_event(
                "team_join_rejected",
                team_id=team_id,
                user_id=user_id,
                code=req.get("code"),
                msg=req.get("msg", "加入失败"),
            )
            await join_team_cmd.finish(_maybe_binding_hint(req.get("msg", "加入失败")))

        data = req.get("data", {})
        notify_members = data.get("notify_members")
        _log_team_event(
            "team_joined",
            team_id=team_id,
            user_id=user_id,
            team=_team_snapshot(data.get("team")),
            notify_member_count=len(notify_members or []),
        )

        team_info = data.get("team") or {}
        members = team_info.get("members") or []
        creator = _find_creator_member(members)
        joined_member = _find_member_by_uid(members, user_id)

        # @队长通知有新队员加入
        if creator and joined_member and str(creator.get("platform_uid")) != str(user_id) and creator.get("platform") == "qq":
            notify_msg = (
                MessageSegment.at(int(creator["platform_uid"]))
                + f" 🎮 你的队伍有新队员加入\n"
                f"加入玩家: {joined_member.get('player_name', '未知')}\n"
                f"加入玩家KD: {joined_member.get('kd', '?')}\n\n"
                f"{_format_team_overview(team_id, team_info, '当前队伍信息')}"
            )
            _log_team_event(
                "team_creator_notified_join",
                team_id=team_id,
                creator=_member_snapshot(creator),
                joined_member=_member_snapshot(joined_member),
            )
            await join_team_cmd.send(notify_msg)

        # 队伍满员，@所有成员
        if notify_members:
            _log_team_event("team_full", team_id=team_id, members=[_member_snapshot(m) for m in notify_members], member_count=len(notify_members))
            full_msg = _build_at_members(notify_members) + "\n" + _format_team_overview(
                team_id,
                {"members": notify_members, "slots_needed": max(len(notify_members) - 1, 0), "slots_remaining": 0},
                "🎮 队伍已满员，已为你匹配到完整队伍",
            )
            await join_team_cmd.send(full_msg)
            await join_team_cmd.finish(f"✅ 已加入队伍 #{team_id}，队伍已满员！")
        else:
            await join_team_cmd.finish(f"✅ 已加入队伍 #{team_id}，等待更多队友加入...")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        _log_team_event("team_join_request_error", team_id=team_id, user_id=user_id, error=str(e))
        await join_team_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        team_logger.exception("team_join_failed | team_id=%s | user_id=%s", team_id, user_id)
        await join_team_cmd.finish(f"❌ 加入出错: {e}")


@cancel_team_cmd.handle()
@team_svc.patch_handler()
async def handle_cancel_team(event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    if not content or not content.isdigit():
        await cancel_team_cmd.finish("⚠️ 请提供队伍ID，如: /取消组队 123")

    team_id = int(content)
    user_id = event.get_user_id()

    try:
        resp = await api_client.cancel_team(team_id=team_id, platform="qq", platform_uid=user_id, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            _log_team_event(
                "team_cancel_rejected",
                team_id=team_id,
                user_id=user_id,
                code=req.get("code"),
                msg=req.get("msg", "取消失败"),
            )
            await cancel_team_cmd.finish(_maybe_binding_hint(req.get("msg", "取消失败")))

        _log_team_event("team_cancelled", team_id=team_id, user_id=user_id)
        await cancel_team_cmd.finish(f"✅ 队伍 #{team_id} 已取消")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        _log_team_event("team_cancel_request_error", team_id=team_id, user_id=user_id, error=str(e))
        await cancel_team_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        team_logger.exception("team_cancel_failed | team_id=%s | user_id=%s", team_id, user_id)
        await cancel_team_cmd.finish(f"❌ 取消出错: {e}")


@leave_team_cmd.handle()
@team_svc.patch_handler()
async def handle_leave_team(event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    if not content or not content.isdigit():
        await leave_team_cmd.finish("⚠️ 请提供队伍ID，如: /退出队伍 123")

    team_id = int(content)
    user_id = event.get_user_id()
    team_data: dict = {}
    creator_before_leave: dict | None = None
    leaving_member_before_leave: dict | None = None

    try:
        team_resp = await api_client.get_team(team_id=team_id, timeout=5.0)
        team_req = team_resp.json()
        if team_req.get("code") == "0000":
            team_data = team_req.get("data") or {}
            members = team_data.get("members") or []
            creator_before_leave = _find_creator_member(members)
            leaving_member_before_leave = _find_member_by_uid(members, user_id)

        resp = await api_client.leave_team(team_id=team_id, platform="qq", platform_uid=user_id, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            _log_team_event(
                "team_leave_rejected",
                team_id=team_id,
                user_id=user_id,
                code=req.get("code"),
                msg=req.get("msg", "退出失败"),
            )
            await leave_team_cmd.finish(_maybe_binding_hint(req.get("msg", "退出失败")))

        _log_team_event("team_left", team_id=team_id, user_id=user_id)

        # @队长通知有队员离开
        if (
            creator_before_leave
            and leaving_member_before_leave
            and str(creator_before_leave.get("platform_uid")) != str(user_id)
            and creator_before_leave.get("platform") == "qq"
        ):
            updated_team_info = {
                "id": team_id,
                "members": [
                    member
                    for member in (team_data.get("members") or [])
                    if str(member.get("platform_uid")) != str(user_id)
                ],
                "slots_needed": team_data.get("slots_needed"),
                "slots_remaining": (team_data.get("slots_remaining") or 0) + 1,
            }
            notify_msg = (
                MessageSegment.at(int(creator_before_leave["platform_uid"]))
                + f" 🎮 你的队伍有队员离开\n"
                f"离开玩家: {leaving_member_before_leave.get('player_name', '未知')}\n"
                f"离开玩家KD: {leaving_member_before_leave.get('kd', '?')}\n\n"
                f"{_format_team_overview(team_id, updated_team_info, '当前队伍信息')}"
            )
            _log_team_event(
                "team_creator_notified_leave",
                team_id=team_id,
                creator=_member_snapshot(creator_before_leave),
                leaving_member=_member_snapshot(leaving_member_before_leave),
            )
            await leave_team_cmd.send(notify_msg)

        await leave_team_cmd.finish(f"✅ 已退出队伍 #{team_id}")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        _log_team_event("team_leave_request_error", team_id=team_id, user_id=user_id, error=str(e))
        await leave_team_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        team_logger.exception("team_leave_failed | team_id=%s | user_id=%s", team_id, user_id)
        await leave_team_cmd.finish(f"❌ 退出出错: {e}")


@invite_cmd.handle()
@team_svc.patch_handler()
async def handle_invite(event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    parts = content.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        await invite_cmd.finish("⚠️ 格式: /邀请 <队伍ID> <玩家昵称>")

    team_id = int(parts[0])
    target_name = parts[1].strip()
    user_id = event.get_user_id()

    try:
        resp = await api_client.invite_player(team_id=team_id, platform="qq", platform_uid=user_id, target_player_name=target_name, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            _log_team_event(
                "team_invite_rejected",
                team_id=team_id,
                user_id=user_id,
                target_name=target_name,
                code=req.get("code"),
                msg=req.get("msg", "邀请失败"),
            )
            await invite_cmd.finish(_maybe_binding_hint(req.get("msg", "邀请失败")))

        data = req.get("data", {})
        target_uid = data.get("platform_uid")
        target_player = data.get("player_name", target_name)
        kd = data.get("kd", "?")

        _log_team_event(
            "team_invited",
            team_id=team_id,
            user_id=user_id,
            target_uid=target_uid,
            target_player=target_player,
            target_kd=kd,
        )

        # @被邀请玩家通知
        if target_uid:
            at_msg = MessageSegment.at(int(target_uid)) + f" 🎮 玩家邀请你加入队伍 #{team_id}\n回复: /接受 {team_id}"
            await invite_cmd.send(at_msg)

        await invite_cmd.finish(f"✅ 已向 {target_player}(KD:{kd}) 发送邀请")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        _log_team_event("team_invite_request_error", team_id=team_id, user_id=user_id, target_name=target_name, error=str(e))
        await invite_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        team_logger.exception("team_invite_failed | team_id=%s | user_id=%s | target_name=%s", team_id, user_id, target_name)
        await invite_cmd.finish(f"❌ 邀请出错: {e}")


@accept_cmd.handle()
@team_svc.patch_handler()
async def handle_accept(event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    if not content or not content.isdigit():
        await accept_cmd.finish("⚠️ 请提供队伍ID，如: /接受 123")

    team_id = int(content)
    user_id = event.get_user_id()

    try:
        resp = await api_client.accept_invite(team_id=team_id, platform="qq", platform_uid=user_id, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            _log_team_event(
                "team_accept_rejected",
                team_id=team_id,
                user_id=user_id,
                code=req.get("code"),
                msg=req.get("msg", "接受失败"),
            )
            await accept_cmd.finish(_maybe_binding_hint(req.get("msg", "接受失败")))

        data = req.get("data", {})
        notify_members = data.get("notify_members")
        _log_team_event(
            "team_invite_accepted",
            team_id=team_id,
            user_id=user_id,
            team=_team_snapshot(data.get("team")),
            notify_member_count=len(notify_members or []),
        )

        team_info = data.get("team") or {}
        members = team_info.get("members") or []
        creator = _find_creator_member(members)
        joined_member = _find_member_by_uid(members, user_id)

        # @队长通知有新队员加入
        if creator and joined_member and str(creator.get("platform_uid")) != str(user_id) and creator.get("platform") == "qq":
            notify_msg = (
                MessageSegment.at(int(creator["platform_uid"]))
                + f" 🎮 你的队伍有新队员加入\n"
                f"加入玩家: {joined_member.get('player_name', '未知')}\n"
                f"加入玩家KD: {joined_member.get('kd', '?')}\n\n"
                f"{_format_team_overview(team_id, team_info, '当前队伍信息')}"
            )
            _log_team_event(
                "team_creator_notified_join",
                team_id=team_id,
                creator=_member_snapshot(creator),
                joined_member=_member_snapshot(joined_member),
            )
            await accept_cmd.send(notify_msg)

        # 队伍满员，@所有成员
        if notify_members:
            _log_team_event("team_full", team_id=team_id, members=[_member_snapshot(m) for m in notify_members], member_count=len(notify_members))
            full_msg = _build_at_members(notify_members) + "\n" + _format_team_overview(
                team_id,
                {"members": notify_members, "slots_needed": max(len(notify_members) - 1, 0), "slots_remaining": 0},
                "🎮 队伍已满员，已为你匹配到完整队伍",
            )
            await accept_cmd.send(full_msg)
            await accept_cmd.finish(f"✅ 已加入队伍 #{team_id}，队伍已满员！")
        else:
            await accept_cmd.finish(f"✅ 已加入队伍 #{team_id}，等待更多队友...")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        _log_team_event("team_accept_request_error", team_id=team_id, user_id=user_id, error=str(e))
        await accept_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        team_logger.exception("team_accept_failed | team_id=%s | user_id=%s", team_id, user_id)
        await accept_cmd.finish(f"❌ 接受出错: {e}")
