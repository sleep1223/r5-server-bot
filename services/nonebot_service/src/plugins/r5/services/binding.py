import traceback

import httpx
from .common import on_command
from nonebot.adapters.onebot.v11 import Event, Message, PrivateMessageEvent
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service

binding_svc = r5_service.create_subservice("binding")
admin_binding_svc = binding_svc.create_subservice("admin")

bind_cmd = on_command("绑定", priority=5, block=True)
unbind_cmd = on_command("解绑", priority=5, block=True)
admin_bind_cmd = on_command("管理绑定", priority=4, block=True)
admin_unbind_cmd = on_command("管理解绑", priority=4, block=True)
my_info_cmd = on_command("我的信息", aliases={"个人信息"}, priority=5, block=True)


def _format_binding_success_message(name: str, app_key: str) -> str:
    msg = "✅ 绑定成功！\n"
    msg += f"游戏昵称: {name}\n"
    msg += f"AppKey: {app_key}\n"
    msg += "已为你启用组队功能。\n"
    msg += "现在可以直接使用: /组队、/组队列表、/加入 <队伍ID>\n"
    msg += "并且 /个人kd 和 /个人武器 不需要再指定名字。\n"
    msg += "请妥善保管 AppKey，用于前端登录认证。\n\n"
    msg += f"🔗 一键登录: https://r5.sleep0.de/teams?appkey={app_key}\n"
    msg += "⚠️ 请勿将此链接发送给他人！"
    return msg


def _format_existing_binding_message(name: str, app_key: str) -> str:
    msg = f"📋 当前绑定信息\n游戏昵称: {name}\nAppKey: {app_key}\n\n"
    msg += f"🔗 一键登录: https://r5.sleep0.de/teams?appkey={app_key}\n"
    msg += "⚠️ 请勿将此链接发送给他人！"
    return msg


@bind_cmd.handle()
@binding_svc.patch_handler()
async def handle_bind(event: Event, args: Message = CommandArg()) -> None:
    player_query = args.extract_plain_text().strip()
    if not player_query:
        await bind_cmd.finish("⚠️ 请提供游戏昵称或ID，如: /绑定 MyName 或 /绑定 10086")

    if not isinstance(event, PrivateMessageEvent):
        await bind_cmd.finish("⚠️ 绑定涉及敏感信息（AppKey），请先添加机器人为好友，然后私信发送：\n/绑定 " + player_query)

    user_id = event.get_user_id()

    try:
        resp = await api_client.bind_player(platform="qq", platform_uid=user_id, player_query=player_query, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            err_msg = req.get("msg", "绑定失败")

            if "已绑定" in err_msg:
                await bind_cmd.send(f"❌ {err_msg}")
                binding_resp = await api_client.get_binding(platform="qq", platform_uid=user_id, timeout=5.0)
                binding_req = binding_resp.json()
                if binding_req.get("code") == "0000":
                    binding_data = binding_req.get("data", {})
                    name = binding_data.get("player_name", "未知")
                    app_key = binding_data.get("app_key", "")
                    await bind_cmd.finish(_format_existing_binding_message(name, app_key))

            await bind_cmd.finish(f"❌ {req.get('msg', '绑定失败')}")

        data = req.get("data", {})
        app_key = data.get("app_key", "")
        name = data.get("player_name", player_query)
        await bind_cmd.finish(_format_binding_success_message(name, app_key))

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await bind_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await bind_cmd.finish(f"❌ 绑定出错: {e}")


@unbind_cmd.handle()
@binding_svc.patch_handler()
async def handle_unbind(event: Event) -> None:
    user_id = event.get_user_id()

    try:
        resp = await api_client.unbind_player(platform="qq", platform_uid=user_id, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            await unbind_cmd.finish(f"❌ {req.get('msg', '解绑失败')}")

        await unbind_cmd.finish("✅ 解绑成功")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await unbind_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await unbind_cmd.finish(f"❌ 解绑出错: {e}")


@admin_bind_cmd.handle()
@admin_binding_svc.patch_handler()
async def handle_admin_bind(args: Message = CommandArg()) -> None:
    """管理员绑定: /管理绑定 <QQ号> <游戏昵称或ID>"""
    text = args.extract_plain_text().strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await admin_bind_cmd.finish("⚠️ 用法: /管理绑定 <QQ号> <游戏昵称或ID>")

    target_qq = parts[0]
    player_query = parts[1]

    if not target_qq.isdigit():
        await admin_bind_cmd.finish("⚠️ QQ号必须是数字")

    try:
        resp = await api_client.admin_bind_player(platform="qq", platform_uid=target_qq, player_query=player_query, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            await admin_bind_cmd.finish(f"❌ {req.get('msg', '绑定失败')}")

        data = req.get("data", {})
        name = data.get("player_name", player_query)
        await admin_bind_cmd.finish(f"✅ 管理员绑定成功\nQQ: {target_qq} → 玩家: {name}")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await admin_bind_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await admin_bind_cmd.finish(f"❌ 绑定出错: {e}")


@admin_unbind_cmd.handle()
@admin_binding_svc.patch_handler()
async def handle_admin_unbind(args: Message = CommandArg()) -> None:
    """管理员解绑: /管理解绑 <QQ号>"""
    target_qq = args.extract_plain_text().strip()
    if not target_qq or not target_qq.isdigit():
        await admin_unbind_cmd.finish("⚠️ 用法: /管理解绑 <QQ号>")

    try:
        resp = await api_client.unbind_player(platform="qq", platform_uid=target_qq, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            await admin_unbind_cmd.finish(f"❌ {req.get('msg', '解绑失败')}")

        await admin_unbind_cmd.finish(f"✅ 已解绑 QQ: {target_qq}")

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await admin_unbind_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await admin_unbind_cmd.finish(f"❌ 解绑出错: {e}")


@my_info_cmd.handle()
@binding_svc.patch_handler()
async def handle_my_info(event: Event) -> None:
    user_id = event.get_user_id()

    try:
        resp = await api_client.get_binding(platform="qq", platform_uid=user_id, timeout=5.0)
        req = resp.json()

        if req.get("code") != "0000":
            await my_info_cmd.finish("❌ 你还未绑定游戏账号，请发送: /绑定 <游戏昵称>")

        data = req.get("data", {})
        name = data.get("player_name", "未知")
        player_id = data.get("player_id", "未知")

        msg = "📋 个人信息\n"
        msg += f"游戏昵称: {name}\n"
        msg += f"玩家ID: {player_id}\n"
        msg += f"平台: QQ ({user_id})"
        await my_info_cmd.finish(msg)

    except FinishedException:
        raise
    except httpx.RequestError as e:
        await my_info_cmd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await my_info_cmd.finish(f"❌ 查询出错: {e}")
