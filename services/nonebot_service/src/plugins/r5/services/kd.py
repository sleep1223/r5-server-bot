import traceback

import httpx
from .common import BINDING_GUIDE, on_command, range_label
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

# Matchers
kd_rank = on_command("kd榜", aliases={"kd ranking", "kd", "kd排行榜"}, priority=5, block=True)
check_kd = on_command("查kd", aliases={"个人kd"}, priority=5, block=True)


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
            kd = p.get("kd", 0)
            kills = p.get("kills", 0)
            msg += f"#{i} {name}: KD {kd} (击杀 {kills})\n"

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
        bind_data = bind_resp.json()
        if bind_data.get("code") == "0000" and bind_data.get("data"):
            target = bind_data["data"].get("player_name", "")
    except Exception:
        pass
    if not target:
        await check_kd.finish(BINDING_GUIDE)

    range_map = {
        "今日": "today", "今天": "today", "today": "today",
        "昨日": "yesterday", "昨天": "yesterday", "yesterday": "yesterday",
        "本周": "week", "week": "week",
        "上周": "last_week", "last_week": "last_week",
        "本月": "month", "month": "month",
        "全部": "all", "all": "all",
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

        summary = req.get("summary")
        if summary:
            tk = summary.get("total_kills", 0)
            td = summary.get("total_deaths", 0)
            tkd = summary.get("kd", 0)
            msg += f"📈 总计: 击杀 {tk} / 死亡 {td} (KD {tkd})\n"

            nemesis = summary.get("nemesis")
            if nemesis:
                n_name = nemesis.get("opponent_name", "Unknown")
                n_kd = nemesis.get("kd")
                n_k = nemesis.get("kills")
                n_d = nemesis.get("deaths")
                msg += f"⚔️ 宿敌: {n_name} ({n_k}/{n_d} - KD {n_kd})\n"

            worst = summary.get("worst_enemy")
            if worst:
                w_name = worst.get("opponent_name", "Unknown")
                w_kd = worst.get("kd")
                w_ekd = worst.get("enemy_kd_display")
                w_k = worst.get("kills")
                w_d = worst.get("deaths")
                # 如果没有 enemy_kd_display (旧接口), 回退到 kd
                if w_ekd is None:
                    msg += f"☠️ 天敌: {w_name} ({w_k}/{w_d} - KD {w_kd})\n"
                else:
                    msg += f"☠️ 天敌: {w_name} ({w_k}/{w_d} - 对敌KD {w_ekd})\n"

            msg += "-" * 30 + "\n"

        msg += "对手 | K/D | 击杀/死亡\n"
        msg += "-" * 30 + "\n"

        # Limit to top 10
        display_data = data[:10]

        for p in display_data:
            op_name = p.get("opponent_name", "Unknown")
            kd = p.get("kd", 0)
            k = p.get("kills", 0)
            d = p.get("deaths", 0)
            msg += f"{op_name}: {kd} ({k}/{d})\n"

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
