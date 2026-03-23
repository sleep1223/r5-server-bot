import traceback

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service

# Service definition
kd_service = r5_service.create_subservice("kd")
rank_service = kd_service.create_subservice("rank")
check_service = kd_service.create_subservice("check")

RANGE_MAP = {
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
    "全部": "all",
    "all": "all",
}

RANGE_DISPLAY = {
    "today": "今日",
    "yesterday": "昨日",
    "week": "本周",
    "month": "本月",
    "all": "全部",
}

SORT_DISPLAY = {
    "kd": "KD",
    "kills": "击杀数",
    "deaths": "死亡数",
}

# Matchers
kd_rank = on_command("kd榜", aliases={"kd ranking", "kd", "kd排行榜"}, priority=5, block=True)
check_kd = on_command("查kd", aliases={"个人kd"}, priority=5, block=True)


def _parse_range(content: str) -> str:
    for k, v in RANGE_MAP.items():
        if k in content:
            return v
    return "today"


def _parse_sort(content: str) -> str:
    if "击杀" in content or "kills" in content:
        return "kills"
    if "死亡" in content or "deaths" in content:
        return "deaths"
    return "kd"


@kd_rank.handle()
@rank_service.patch_handler()
async def handle_kd_rank(args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()

    range_type = _parse_range(content)
    sort = _parse_sort(content)

    base_min_kills = 100
    dynamic_min_kills = base_min_kills if range_type in ["today", "yesterday"] else base_min_kills * 3

    try:
        resp = await api_client.get_kd_leaderboard(
            range_type=range_type,
            page_size=20,
            sort=sort,
            min_kills=dynamic_min_kills,
            timeout=3.0,
        )

        if resp.status_code != 200:
            await kd_rank.finish(f"查询失败，服务器返回 HTTP {resp.status_code}")

        req = resp.json()
        if req.get("code") != "0000":
            await kd_rank.finish(f"查询失败: {req.get('msg')}")

        data = req.get("data", [])
        range_cn = RANGE_DISPLAY.get(range_type, range_type)
        sort_cn = SORT_DISPLAY.get(sort, sort)

        if not data:
            await kd_rank.finish(f"暂无 {range_cn} KD 排行数据")

        msg = f"KD 排行榜（{range_cn}）\n"
        msg += f"筛选：至少 {dynamic_min_kills} 击杀 | 排序：{sort_cn}\n"
        msg += "━" * 24 + "\n"

        for i, p in enumerate(data, 1):
            name = p.get("name", "未知")
            kd = p.get("kd", 0)
            kills = p.get("kills", 0)
            deaths = p.get("deaths", 0)
            msg += f" {i:>2}. {name}\n"
            msg += f"     KD {kd}  击杀 {kills}  死亡 {deaths}\n"

        msg += "━" * 24 + "\n"
        msg += "服务器面板：https://r5.sleep0.de"
        await kd_rank.finish(msg.strip())

    except FinishedException:
        ...
    except httpx.RequestError as e:
        await kd_rank.finish(f"网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await kd_rank.finish(f"查询出错: {e}")


@check_kd.handle()
@check_service.patch_handler()
async def handle_check_kd(args: Message = CommandArg()) -> None:
    target = args.extract_plain_text().strip()
    if not target:
        await check_kd.finish("请提供玩家名称或 ID\n用法：/查kd <玩家名或ID>")

    sort = _parse_sort(target)

    try:
        resp = await api_client.get_player_vs_all(target, sort=sort, timeout=3.0)

        if resp.status_code != 200:
            await check_kd.finish(f"查询失败，服务器返回 HTTP {resp.status_code}")

        req = resp.json()

        if req.get("code") == "2001":
            await check_kd.finish(f"未找到玩家「{target}」")

        if req.get("code") != "0000":
            await check_kd.finish(f"查询失败: {req.get('msg')}")

        data = req.get("data", [])
        if not data:
            await check_kd.finish(f"玩家「{target}」暂无对战记录")

        player_info = req.get("player") or {}
        player_name = player_info.get("name") or target

        msg = f"「{player_name}」对战数据\n"

        if player_info:
            country = player_info.get("country") or "未知"
            region = player_info.get("region") or "未知"
            msg += f"地区：{country} {region}\n"

        summary = req.get("summary")
        if summary:
            tk = summary.get("total_kills", 0)
            td = summary.get("total_deaths", 0)
            tkd = summary.get("kd", 0)
            msg += f"总计：击杀 {tk} / 死亡 {td}（KD {tkd}）\n"

            nemesis = summary.get("nemesis")
            if nemesis:
                n_name = nemesis.get("opponent_name", "未知")
                n_kd = nemesis.get("kd")
                n_k = nemesis.get("kills")
                n_d = nemesis.get("deaths")
                msg += f"宿敌：{n_name}（{n_k}/{n_d} KD {n_kd}）\n"

            worst = summary.get("worst_enemy")
            if worst:
                w_name = worst.get("opponent_name", "未知")
                w_kd = worst.get("kd")
                w_ekd = worst.get("enemy_kd_display")
                w_k = worst.get("kills")
                w_d = worst.get("deaths")
                if w_ekd is not None:
                    msg += f"天敌：{w_name}（{w_k}/{w_d} 对敌KD {w_ekd}）\n"
                else:
                    msg += f"天敌：{w_name}（{w_k}/{w_d} KD {w_kd}）\n"

        msg += "━" * 24 + "\n"
        msg += "对手 | KD | 击杀/死亡\n"
        msg += "━" * 24 + "\n"

        display_data = data[:20]
        for p in display_data:
            op_name = p.get("opponent_name", "未知")
            kd = p.get("kd", 0)
            k = p.get("kills", 0)
            d = p.get("deaths", 0)
            msg += f"{op_name}：KD {kd}（{k}/{d}）\n"

        if len(data) > 20:
            msg += f"\n…及其他 {len(data) - 20} 名对手"

        msg += f"\n详细数据：https://r5.sleep0.de/player/{player_name}"
        await check_kd.finish(msg.strip())

    except FinishedException:
        ...
    except httpx.RequestError as e:
        await check_kd.finish(f"网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await check_kd.finish(f"查询出错: {e}")
