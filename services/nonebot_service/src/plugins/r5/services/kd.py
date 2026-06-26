import traceback

import httpx
from .common import BINDING_GUIDE, format_input_device, format_input_device_emoji, on_command, range_label
from nonebot.adapters.onebot.v11 import Event, Message
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service
from .server_arg import pop_server_arg

# Service definition
kd_service = r5_service.create_subservice("kd")
rank_service = kd_service.create_subservice("rank")
check_service = kd_service.create_subservice("check")
device_rank_service = kd_service.create_subservice("input_device_rank")

# Matchers
kd_rank = on_command("kd榜", aliases={"kd ranking", "kd", "kd排行榜"}, priority=5, block=True)
check_kd = on_command("查kd", aliases={"个人kd"}, priority=5, block=True)
input_device_rank = on_command("设备榜", aliases={"输入设备榜", "输入设备排行", "手柄榜", "键鼠榜"}, priority=5, block=True)


def _parse_input_device_filter(content: str) -> str | None:
    lowered = content.lower()
    if "手柄" in content or "controller" in lowered or "gamepad" in lowered or "pad" in lowered:
        return "controller"
    if "键鼠" in content or "键盘" in content or "鼠标" in content or "keyboard" in lowered or "kbm" in lowered or "mnk" in lowered:
        return "keyboard_mouse"
    if "未知" in content or "unknown" in lowered:
        return "unknown"
    return None


@kd_rank.handle()
@rank_service.patch_handler()
async def handle_kd_rank(args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    content, server_arg = pop_server_arg(content)

    range_map = {
        "今日": "today",
        "今天": "today",
        "today": "today",
        "昨日": "yesterday",
        "昨天": "yesterday",
        "yesterday": "yesterday",
        "本周": "week",
        "week": "week",
        "本月": "month",
        "month": "month",
    }

    range_type = "today"
    for k, v in range_map.items():
        if k in content:
            range_type = v
            break

    # Default params
    base_min_kills = 100
    dynamic_min_kills = base_min_kills if range_type in ["today", "yesterday"] else base_min_kills * 3
    params: dict = {
        "range_type": range_type,
        "page_size": 10,
        "sort": "kd",
        "min_kills": dynamic_min_kills,
    }
    if server_arg:
        params["server"] = server_arg

    # Parse sort from content
    if "击杀" in content or "kills" in content:
        params["sort"] = "kills"
    elif "死亡" in content or "deaths" in content:
        params["sort"] = "deaths"

    try:
        resp = await api_client.get_kd_leaderboard(**params, timeout=3.0)

        if resp.status_code != 200:
            await kd_rank.finish(f"❌ 查询失败: HTTP {resp.status_code}")
        req = resp.json()

        if req.get("code") == "4002":
            await kd_rank.finish(f"❌ 未找到服务器: {server_arg}")

        data = req.get("data", [])

        if not data:
            await kd_rank.finish(f"ℹ️ 暂无数据 ({range_label(range_type)})")

        # Format message
        server_info = req.get("server") or {}
        scope = server_info.get("short_name") or server_info.get("name") or server_info.get("host")
        title_suffix = f" @{scope}" if scope else ""
        msg = f"🏆 R5 KD排行榜 ({range_label(range_type)}){title_suffix}\n"
        msg += f"筛选: 至少 {params['min_kills']} 击杀\t排序: {params['sort']}\n"
        msg += "排名 | 玩家 | K/D | 击杀数\n"
        msg += "-" * 30 + "\n"

        for i, p in enumerate(data, 1):
            name = p.get("name", "Unknown")
            device = format_input_device_emoji(p.get("input_device"))
            kd = p.get("kd", 0)
            kills = p.get("kills", 0)
            msg += f"#{i} {name} [{device}]: KD {kd} (击杀 {kills})\n"

        msg += "\n🖥️ 在线服务器面板: https://r5.sleep0.de"
        await kd_rank.finish(msg.strip())

    except FinishedException:
        ...
    except httpx.RequestError as e:
        await kd_rank.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await kd_rank.finish(f"❌ 查询出错: {e}")


@check_kd.handle()
@check_service.patch_handler()
async def handle_check_kd(event: Event, args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    content, server_arg = pop_server_arg(content)

    # 必须通过绑定获取玩家
    target = ""
    user_id = event.get_user_id()
    try:
        bind_resp = await api_client.get_binding(platform="qq", platform_uid=user_id, timeout=5.0)
        if bind_resp.status_code != 200:
            await check_kd.finish(f"❌ 查询绑定失败: HTTP {bind_resp.status_code}")
        bind_data = bind_resp.json()
    except httpx.TimeoutException:
        await check_kd.finish("❌ 网络请求超时，请稍后再试")
    except httpx.RequestError as e:
        await check_kd.finish(f"❌ 网络请求错误: {e}")
    except ValueError:
        await check_kd.finish("❌ 查询绑定失败: 后端返回异常")

    if bind_data.get("code") == "0000" and bind_data.get("data"):
        target = bind_data["data"].get("player_name", "")
    elif bind_data.get("code") != "6003":
        await check_kd.finish(f"❌ 查询绑定失败: {bind_data.get('msg', '未知错误')}")
    if not target:
        await check_kd.finish(BINDING_GUIDE)

    range_map = {
        "今日": "today",
        "今天": "today",
        "today": "today",
        "昨日": "yesterday",
        "昨天": "yesterday",
        "yesterday": "yesterday",
        "本周": "week",
        "week": "week",
        "上周": "last_week",
        "last_week": "last_week",
        "本月": "month",
        "month": "month",
        "全部": "all",
        "all": "all",
    }
    range_type = "month"
    for k, v in range_map.items():
        if k in content:
            range_type = v
            break

    sort = "kd"
    if "击杀" in content or "kills" in content:
        sort = "kills"
    elif "死亡" in content or "deaths" in content:
        sort = "deaths"

    try:
        resp = await api_client.get_player_vs_all(target, sort=sort, server=server_arg, range_type=range_type, timeout=3.0)

        if resp.status_code != 200:
            await check_kd.finish(f"❌ 查询失败: HTTP {resp.status_code}")
        req = resp.json()

        if req.get("code") == "4001":
            await check_kd.finish(f"❌ 未找到玩家: {target}")
        if req.get("code") == "4002":
            await check_kd.finish(f"❌ 未找到服务器: {server_arg}")

        data = req.get("data", [])
        if not data:
            await check_kd.finish(f"ℹ️ 玩家 {target} 暂无对战记录")

        player_info = req.get("player")
        player_name = player_info.get("name") or target

        # Format message
        server_info = req.get("server") or {}
        scope = server_info.get("short_name") or server_info.get("name") or server_info.get("host")
        title_suffix = f" @{scope}" if scope else ""
        msg = f"📊 {player_name} 对战数据 ({range_label(range_type)}){title_suffix}\n"

        if player_info:
            country = player_info.get("country") or "未知"
            region = player_info.get("region") or "未知"
            msg += f"📍 地区: {country} / {region}\n"
            msg += f"🎮 输入设备: {format_input_device(player_info.get('input_device'))}\n"

        summary = req.get("summary")
        if summary:
            tk = summary.get("total_kills", 0)
            td = summary.get("total_deaths", 0)
            tkd = summary.get("kd", 0)
            msg += f"📈 总计: 击杀 {tk} / 死亡 {td} (KD {tkd})\n"

            nemesis = summary.get("nemesis")
            if nemesis:
                n_name = nemesis.get("opponent_name", "Unknown")
                n_device = format_input_device(nemesis.get("input_device"))
                n_kd = nemesis.get("kd")
                n_k = nemesis.get("kills")
                n_d = nemesis.get("deaths")
                msg += f"⚔️ 宿敌: {n_name} [{n_device}] ({n_k}/{n_d} - KD {n_kd})\n"

            worst = summary.get("worst_enemy")
            if worst:
                w_name = worst.get("opponent_name", "Unknown")
                w_device = format_input_device(worst.get("input_device"))
                w_ekd = worst.get("enemy_kd_display")
                w_k = worst.get("kills")
                w_d = worst.get("deaths")
                msg += f"☠️ 天敌: {w_name} [{w_device}] ({w_k}/{w_d} - 对敌KD {w_ekd})\n"

            msg += "-" * 30 + "\n"

        msg += "对手 | K/D | 击杀/死亡\n"
        msg += "-" * 30 + "\n"

        # Limit to top 10
        display_data = data[:10]

        for p in display_data:
            op_name = p.get("opponent_name", "Unknown")
            op_device = format_input_device(p.get("input_device"))
            kd = p.get("kd", 0)
            k = p.get("kills", 0)
            d = p.get("deaths", 0)
            msg += f"{op_name} [{op_device}]: {kd} ({k}/{d})\n"

        if len(data) > 10:
            msg += f"\n... 以及其他 {len(data) - 10} 名玩家"

        msg += f"\n🖥️ 详细数据: https://r5.sleep0.de/player/{player_name}"
        await check_kd.finish(msg.strip())

    except FinishedException:
        ...
    except httpx.RequestError as e:
        await check_kd.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await check_kd.finish(f"❌ 查询出错: {e}")


@input_device_rank.handle()
@device_rank_service.patch_handler()
async def handle_input_device_rank(args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    content, server_arg = pop_server_arg(content)

    range_map = {
        "今日": "today",
        "今天": "today",
        "today": "today",
        "昨日": "yesterday",
        "昨天": "yesterday",
        "yesterday": "yesterday",
        "本周": "week",
        "week": "week",
        "本月": "month",
        "month": "month",
    }
    range_type = "today"
    for k, v in range_map.items():
        if k in content:
            range_type = v
            break

    sort = "kills"
    if "kd" in content.lower():
        sort = "kd"
    elif "死亡" in content or "deaths" in content:
        sort = "deaths"

    input_device = _parse_input_device_filter(content)
    base_min_kills = 1
    params: dict = {
        "range_type": range_type,
        "page_size": 10,
        "sort": sort,
        "min_kills": base_min_kills,
        "input_device": input_device,
    }
    if server_arg:
        params["server"] = server_arg

    try:
        resp = await api_client.get_kd_leaderboard(**params, timeout=3.0)
        if resp.status_code != 200:
            await input_device_rank.finish(f"❌ 查询失败: HTTP {resp.status_code}")
        req = resp.json()
        if req.get("code") == "4002":
            await input_device_rank.finish(f"❌ 未找到服务器: {server_arg}")

        data = req.get("data", [])
        if not data:
            await input_device_rank.finish(f"ℹ️ 暂无设备 KD 数据 ({range_label(range_type)})")

        server_info = req.get("server") or {}
        scope = server_info.get("short_name") or server_info.get("name") or server_info.get("host")
        title_suffix = f" @{scope}" if scope else ""
        device_suffix = f" - {format_input_device_emoji(input_device)}" if input_device else ""
        msg = f"🏆 R5 输入设备 KD 榜 ({range_label(range_type)}{device_suffix}){title_suffix}\n"
        msg += f"排序: {sort}\n"
        msg += "排名 | 玩家 | 设备 | 击杀/死亡 | KD\n"
        msg += "-" * 30 + "\n"

        for i, p in enumerate(data[:10], 1):
            name = p.get("name", "Unknown")
            device = format_input_device_emoji(p.get("input_device"))
            kills = p.get("kills", 0)
            deaths = p.get("deaths", 0)
            kd = p.get("kd", 0)
            msg += f"#{i} {name} [{device}]: {kills}/{deaths} KD {kd}\n"

        msg += "\n🖥️ 在线服务器面板: https://r5.sleep0.de"
        await input_device_rank.finish(msg.strip())
    except FinishedException:
        ...
    except httpx.RequestError as e:
        await input_device_rank.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await input_device_rank.finish(f"❌ 查询出错: {e}")
