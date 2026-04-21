import contextlib
import re
import traceback
from datetime import datetime

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Event, Message
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service
from .server_arg import pop_server_arg

match_service = r5_service.create_subservice("match")
recent_service = match_service.create_subservice("recent")
personal_service = match_service.create_subservice("personal")
competitive_service = match_service.create_subservice("competitive")

recent_matches = on_command("对局", aliases={"最近对局", "recent matches"}, priority=5, block=True)
personal_matches = on_command(
    "个人对局", aliases={"我的对局", "personal matches"}, priority=5, block=True
)
competitive_rank = on_command(
    "竞技", aliases={"竞技榜", "competitive"}, priority=5, block=True
)


def _fmt_time(iso_str: str | None) -> str:
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%m-%d %H:%M")
    except ValueError:
        return iso_str


@recent_matches.handle()
@recent_service.patch_handler()
async def handle_recent_matches(args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    _, server_arg = pop_server_arg(content)

    try:
        resp = await api_client.get_recent_matches(limit=10, server=server_arg, timeout=5.0)

        if resp.status_code != 200:
            await recent_matches.finish(f"❌ 查询失败: HTTP {resp.status_code}")
        req = resp.json()

        if req.get("code") == "4002":
            await recent_matches.finish(f"❌ 未找到服务器: {server_arg}")

        data = req.get("data") or []
        threshold = req.get("min_top_kills", 50)

        server_info = req.get("server") or {}
        scope = server_info.get("short_name") or server_info.get("name") or server_info.get("host")
        title_suffix = f" @{scope}" if scope else ""

        if not data:
            await recent_matches.finish(f"ℹ️ 暂无对局数据（top1 击杀需 ≥ {threshold}）{title_suffix}")

        msg = f"🎮 最近对局 top1（击杀 ≥ {threshold}）{title_suffix}\n"
        msg += "-" * 30 + "\n"
        for i, m in enumerate(data, 1):
            top = m.get("top") or {}
            top_name = top.get("name", "Unknown")
            top_kills = top.get("kills", 0)
            top_deaths = top.get("deaths", 0)
            top_kd = top.get("kd", 0)
            map_name = m.get("map_name") or "?"
            ended = _fmt_time(m.get("ended_at"))
            srv = (m.get("server") or {})
            srv_tag = srv.get("short_name") or srv.get("name") or srv.get("host") or ""
            srv_prefix = f"@{srv_tag} " if srv_tag and not scope else ""
            msg += f"#{i} [{ended}] {srv_prefix}{map_name}\n"
            msg += f"    👑 {top_name}: {top_kills}击杀/{top_deaths}死亡 (KD {top_kd})\n"

        msg += "\n🖥️ 面板: https://r5.sleep0.de"
        await recent_matches.finish(msg.strip())

    except FinishedException:
        ...
    except httpx.RequestError as e:
        await recent_matches.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await recent_matches.finish(f"❌ 查询出错: {e}")


@personal_matches.handle()
@personal_service.patch_handler()
async def handle_personal_matches(event: Event, args: Message = CommandArg()) -> None:
    raw = args.extract_plain_text().strip()
    content, server_arg = pop_server_arg(raw)

    # 解析 sort（内嵌文本里的关键字，和 /查kd 风格一致）
    sort = "time"
    if "击杀" in content or "kills" in content:
        sort = "kills"
        content = content.replace("击杀", "").replace("kills", "").strip()
    elif "kd" in content.lower():
        sort = "kd"
        content = content.replace("kd", "").replace("KD", "").strip()
    elif "时间" in content or "time" in content:
        sort = "time"
        content = content.replace("时间", "").replace("time", "").strip()

    target = content.strip()
    if not target:
        # 未给玩家：尝试从 qq 绑定取
        user_id = event.get_user_id()
        try:
            bind_resp = await api_client.get_binding(platform="qq", platform_uid=user_id, timeout=5.0)
            bind_data = bind_resp.json()
            if bind_data.get("code") == "0000" and bind_data.get("data"):
                target = bind_data["data"].get("player_name", "")
        except Exception:
            pass
        if not target:
            await personal_matches.finish("⚠️ 请提供玩家名称或ID，或先 /绑定 账号")

    try:
        resp = await api_client.get_player_matches(target, sort=sort, server=server_arg, timeout=5.0)
        if resp.status_code != 200:
            await personal_matches.finish(f"❌ 查询失败: HTTP {resp.status_code}")
        req = resp.json()

        code = req.get("code")
        if code == "2001":
            await personal_matches.finish(f"❌ 未找到玩家: {target}")
        if code == "4002":
            await personal_matches.finish(f"❌ 未找到服务器: {server_arg}")

        data = req.get("data") or []
        player_info = req.get("player") or {}
        player_name = player_info.get("name") or target

        server_info = req.get("server") or {}
        scope = server_info.get("short_name") or server_info.get("name") or server_info.get("host")
        title_suffix = f" @{scope}" if scope else ""

        if not data:
            await personal_matches.finish(f"ℹ️ {player_name} 暂无对局记录{title_suffix}")

        sort_label = {"time": "时间", "kills": "击杀", "kd": "KD"}.get(sort, sort)
        msg = f"📋 {player_name} 最近 {len(data)} 场对局（按{sort_label}）{title_suffix}\n"
        msg += "-" * 30 + "\n"
        for i, m in enumerate(data, 1):
            map_name = m.get("map_name") or "?"
            ended = _fmt_time(m.get("ended_at"))
            k = m.get("kills", 0)
            d = m.get("deaths", 0)
            kd = m.get("kd", 0)
            srv = m.get("server") or {}
            srv_tag = srv.get("short_name") or srv.get("name") or srv.get("host") or ""
            srv_prefix = f"@{srv_tag} " if srv_tag and not scope else ""
            msg += f"#{i} [{ended}] {srv_prefix}{map_name}\n"
            msg += f"    {k}击杀/{d}死亡 (KD {kd})\n"

        msg += f"\n🖥️ 详细数据: https://r5.sleep0.de/player/{player_name}"
        await personal_matches.finish(msg.strip())

    except FinishedException:
        ...
    except httpx.RequestError as e:
        await personal_matches.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await personal_matches.finish(f"❌ 查询出错: {e}")


_COMPETITIVE_RANGE_MAP = {
    "今日": "today",
    "今天": "today",
    "today": "today",
    "本周": "week",
    "这周": "week",
    "week": "week",
    "this_week": "week",
    "上周": "last_week",
    "last week": "last_week",
    "last_week": "last_week",
}


@competitive_rank.handle()
@competitive_service.patch_handler()
async def handle_competitive(args: Message = CommandArg()) -> None:
    content = args.extract_plain_text().strip()
    content, server_arg = pop_server_arg(content)

    # 时间范围：命中任一关键字即采用；默认今日
    lower = content.lower()
    range_type = "today"
    for k, v in _COMPETITIVE_RANGE_MAP.items():
        if k in content or k in lower:
            range_type = v
            break

    # 分页：支持 "第N页" 或裸数字
    page_no = 1
    m = re.search(r"第\s*(\d+)\s*页", content) or re.search(r"\b(\d+)\b", content)
    if m:
        with contextlib.suppress(ValueError):
            page_no = max(1, int(m.group(1)))

    try:
        resp = await api_client.get_competitive_leaderboard(
            range_type=range_type,
            page_no=page_no,
            page_size=20,
            server=server_arg,
            timeout=6.0,
        )
        if resp.status_code != 200:
            await competitive_rank.finish(f"❌ 查询失败: HTTP {resp.status_code}")
        req = resp.json()

        if req.get("code") == "4002":
            await competitive_rank.finish(f"❌ 未找到服务器: {server_arg}")

        data = req.get("data") or []
        total = req.get("total", 0)
        top_per_day = req.get("top_per_day", 3)

        server_info = req.get("server") or {}
        scope = server_info.get("short_name") or server_info.get("name") or server_info.get("host")
        title_suffix = f" @{scope}" if scope else ""

        range_label = {"today": "今日", "week": "本周", "last_week": "上周"}.get(range_type, range_type)

        if not data:
            await competitive_rank.finish(f"ℹ️ 暂无竞技数据（{range_label}）{title_suffix}")

        msg = f"🏆 R5 竞技榜 ({range_label}){title_suffix}\n"
        msg += f"规则: 每人每天取击杀前 {top_per_day} 场求和\n"
        msg += f"共 {total} 人 | 第 {page_no} 页\n"
        msg += "-" * 30 + "\n"
        msg += "排名 | 玩家 | 总击杀 (场数)\n"
        msg += "-" * 30 + "\n"

        rank_base = (page_no - 1) * 20
        for i, p in enumerate(data, 1):
            name = p.get("name", "Unknown")
            total_kills = p.get("total_kills", 0)
            counted = p.get("counted_matches", 0)
            msg += f"#{rank_base + i} {name}: {total_kills} 击杀 ({counted} 场)\n"

        msg += "\n🖥️ 面板: https://r5.sleep0.de"
        await competitive_rank.finish(msg.strip())

    except FinishedException:
        ...
    except httpx.RequestError as e:
        await competitive_rank.finish(f"❌ 网络请求错误: {e}")
    except Exception as e:
        traceback.print_exc()
        await competitive_rank.finish(f"❌ 查询出错: {e}")
