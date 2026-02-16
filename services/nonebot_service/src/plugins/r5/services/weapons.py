import traceback

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service

weapons_service = r5_service.create_subservice("weapons")
check_service = weapons_service.create_subservice("check")
lb_service = weapons_service.create_subservice("leaderboard")

check_weapons = on_command("个人武器", aliases={"个人武器"}, priority=5, block=True)
weapon_leaderboard = on_command(
    "武器", aliases={"武器排行", "枪械"}, priority=5, block=True
)

weapon_map = {
    "alternator": "转换者冲锋枪",
    "charge rifle": "充能步枪",
    "devotion": "专注冲锋枪",
    "epg": "EPG",
    "eva8": "EVA8",
    "flatline": "平行步枪",
    "g7": "G7侦察枪",
    "havoc": "哈沃克步枪",
    "hemlok": "赫姆洛克突击步枪",
    "kraber": "克雷贝尔狙击枪",
    "longbow": "长弓狙击步枪",
    "lstar": "L-STAR能量机枪",
    "mastiff": "敖犬霰弹枪",
    "mozambique": "莫桑比克",
    "p2020": "P2020",
    "peacekeeper": "和平捍卫者",
    "prowler": "猎兽冲锋枪",
    "r301": "R301步枪",
    "r99": "R99冲锋枪",
    "re45": "RE45手枪",
    "smart pistol": "智慧手枪",
    "spitfire": "喷火轻机枪",
    "triple take": "三重式狙击枪",
    "wingman": "辅助手枪",
    "volt": "电能冲锋枪",
    "player": "近战",
}


@check_weapons.handle()
@check_service.patch_handler()
async def handle_check_weapons(args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    target = content
    if not target:
        await check_weapons.finish("⚠️ 请提供玩家名称或ID")

    sort = "kd"
    if "击杀" in content:
        sort = "kills"
    elif "死亡" in content:
        sort = "deaths"

    try:
        resp = await api_client.get_player_weapons(
            target=target, sort=sort, timeout=3.0
        )
        if resp.status_code != 200:
            await check_weapons.finish(f"❌ 查询失败: HTTP {resp.status_code}")
        req = resp.json()

        if req.get("code") == "4001":
            await check_weapons.finish(f"❌ 未找到玩家: {target}")

        data = req.get("data", [])
        if not data:
            await check_weapons.finish(f"ℹ️ 玩家 {target} 暂无武器数据")

        player_info = req.get("player") or {}
        player_name = player_info.get("name") or target

        msg = f"🔫 {player_name} 武器统计\n"

        if player_info:
            country = player_info.get("country") or "未知"
            region = player_info.get("region") or "未知"
            msg += f"📍 地区: {country} / {region}\n"

        summary = req.get("summary") or {}
        tk = summary.get("total_kills", 0)
        td = summary.get("total_deaths", 0)
        tkd = summary.get("kd", 0)
        msg += f"📈 总计: 击杀 {tk} / 死亡 {td} (KD {tkd})\n"
        msg += "-" * 30 + "\n"

        msg += "武器 | K/D | 击杀/死亡\n"
        msg += "-" * 30 + "\n"

        display = data[:20]
        for w in display:
            weapon_key = w.get("weapon", "unknown")
            weapon = weapon_map.get(weapon_key.lower(), weapon_key)
            kd = w.get("kd", 0)
            k = w.get("kills", 0)
            d = w.get("deaths", 0)
            msg += f"{weapon}: {kd} ({k}/{d})\n"

        if len(data) > 20:
            msg += f"\n... 以及其他 {len(data) - 20} 个武器"

        msg += f"\n🖥️ 详细数据: https://r5.sleep0.de/player/{player_name}"
        await check_weapons.finish(msg.strip())
    except FinishedException:
        raise
    except httpx.RequestError as e:
        await check_weapons.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await check_weapons.finish(f"❌ 查询出错: {e}")


@weapon_leaderboard.handle()
@lb_service.patch_handler()
async def handle_weapon_leaderboard(args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    sort = "kd"
    if "击杀" in content:
        sort = "kills"
    elif "死亡" in content:
        sort = "deaths"

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
        "全部": "all",
        "all": "all",
    }

    range_type = "today"
    for k, v in range_map.items():
        if k in content:
            range_type = v
            break

    try:
        base_min_kills = 10
        dynamic_min_kills = (
            base_min_kills
            if range_type in ["today", "yesterday"]
            else base_min_kills * 3
        )
        params = {
            "weapon": ["r99", "volt", "wingman", "flatline", "r301", "player"],
            "range_type": range_type,
            "page_no": 1,
            "page_size": 20,
            "sort": sort,
            "min_kills": dynamic_min_kills,
            "min_deaths": 0,
        }
        resp = await api_client.get_weapon_leaderboard(**params, timeout=3.0)
        if resp.status_code != 200:
            await weapon_leaderboard.finish(f"❌ 查询失败: HTTP {resp.status_code}")
        req = resp.json()
        data = req.get("data", [])
        total = req.get("total", 0)
        msg = "🔫 武器排行榜\n"
        msg += "-" * 30 + "\n"

        # Format message
        msg = f"🏆 R5 武器排行榜 ({range_type})\n"
        msg += f"筛选: 至少 {params['min_kills']} 击杀\t排序: {params['sort']}\n"
        msg += "武器 | 最佳玩家 | K/D | 击杀数\n"
        msg += "-" * 30 + "\n"

        if not data:
            msg += "ℹ️ 暂无数据"
            await weapon_leaderboard.finish(msg.strip())
        display = data[:20]
        for w in display:
            weapon_key = w.get("weapon", "unknown")
            weapon_name = weapon_map.get(weapon_key.lower(), weapon_key)
            name = w.get("name") or "未知"
            k = w.get("kills", 0)
            d = w.get("deaths", 0)
            kd = w.get("kd", 0)
            msg += f"{weapon_name}: {name} KD {kd} ({k}/{d})\n"
        if total > len(display):
            msg += f"\n... 以及其他 {total - len(display)} 个武器"

        msg += "\n🖥️ 在线服务器面板: https://r5.sleep0.de"
        await weapon_leaderboard.finish(msg.strip())
    except FinishedException:
        raise
    except httpx.RequestError as e:
        await weapon_leaderboard.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await weapon_leaderboard.finish(f"❌ 查询出错: {e}")
