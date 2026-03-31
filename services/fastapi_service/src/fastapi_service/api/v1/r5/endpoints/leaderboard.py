from typing import Literal

from fastapi import APIRouter, Query
from shared_lib.models import Player, PlayerKilled
from tortoise.functions import Count

from ..constants import WEAPON_NAME_MAP, to_display_weapon, to_internal_weapon
from ..response import paginated, success
from ..utils import calc_kd, get_date_range
from .players import get_player_by_identifier

router = APIRouter()


@router.get("/leaderboard/kd")
async def get_kd_leaderboard(
    range: Literal["today", "yesterday", "week", "month"] = "today",
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    sort: Literal["kills", "deaths", "kd"] = "kd",
    min_kills: int = 100,
    min_deaths: int = 0,
):
    """获取全局 KD 排行榜。"""
    start_time, end_time = get_date_range(range)

    filters = {}
    if start_time:
        filters["created_at__gte"] = start_time
    if end_time:
        filters["created_at__lte"] = end_time

    kills_qs = PlayerKilled.filter(**filters, attacker_id__isnull=False)
    kills_data = await kills_qs.group_by("attacker_id").annotate(k_count=Count("id")).values("attacker_id", "k_count")

    deaths_qs = PlayerKilled.filter(**filters, victim_id__isnull=False)
    deaths_data = await deaths_qs.group_by("victim_id").annotate(d_count=Count("id")).values("victim_id", "d_count")

    stats = {}

    for k in kills_data:
        pid = k["attacker_id"]
        if pid not in stats:
            stats[pid] = {"kills": 0, "deaths": 0}
        stats[pid]["kills"] = k["k_count"]

    for d in deaths_data:
        pid = d["victim_id"]
        if pid not in stats:
            stats[pid] = {"kills": 0, "deaths": 0}
        stats[pid]["deaths"] = d["d_count"]

    if not stats:
        return paginated(data=[], total=0, msg=f"KD Leaderboard for {range} range")

    player_ids = list(stats.keys())
    players = await Player.filter(id__in=player_ids).values("id", "name", "nucleus_id")
    p_map = {p["id"]: p for p in players}

    results = []
    for pid, data in stats.items():
        p_info = p_map.get(pid)
        name = p_info["name"] if p_info else f"Unknown ({pid})"
        nucleus_id = p_info["nucleus_id"] if p_info else None

        kills = data["kills"]
        deaths = data["deaths"]
        kd = calc_kd(kills, deaths)

        if kills < min_kills or deaths < min_deaths:
            continue

        results.append({"name": name, "nucleus_id": nucleus_id, "kills": kills, "deaths": deaths, "kd": kd})

    if sort == "kd":
        results.sort(key=lambda x: (x["kd"], x["kills"]), reverse=True)
    elif sort == "kills":
        results.sort(key=lambda x: x["kills"], reverse=True)
    elif sort == "deaths":
        results.sort(key=lambda x: x["deaths"], reverse=True)

    total = len(results)
    offset = (page_no - 1) * page_size
    paged_results = results[offset : offset + page_size]

    return paginated(data=paged_results, total=total, msg=f"KD Leaderboard for {range} range")


@router.get("/leaderboard/weapon")
async def get_weapon_leaderboard(
    weapon: list[str] = Query(
        default=["r99", "volt", "wingman", "flatline", "r301", "player"],
        description="Weapon names (e.g., r301) or internal codes; multiple allowed",
    ),
    range: Literal["today", "yesterday", "week", "month"] = "today",
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    sort: Literal["kills", "deaths", "kd"] = "kd",
    min_kills: int = 1,
    min_deaths: int = 0,
):
    """获取武器列表的最佳使用者排行榜（默认按 KD 排序），默认时间范围为今日。"""
    start_time, end_time = get_date_range(range)

    filters = {}
    if start_time and end_time:
        filters["created_at__range"] = (start_time, end_time)

    internal_weapons = [to_internal_weapon(w) for w in (weapon or [])]
    internal_weapons = [w for w in internal_weapons if w]
    if not internal_weapons:
        return paginated(data=[], total=0, msg="Invalid weapon(s)")

    display_weapons = [to_display_weapon(w) for w in internal_weapons]
    display_weapons_str = ", ".join(display_weapons)
    winners = []
    if internal_weapons:
        stats_by_weapon = {iw: {} for iw in internal_weapons}
        kills_data = await (
            PlayerKilled
            .filter(**filters, attacker_id__not_isnull=True, weapon__in=internal_weapons)
            .group_by("weapon", "attacker_id")
            .annotate(k_count=Count("id"))
            .values("weapon", "attacker_id", "k_count")
        )
        deaths_data = await (
            PlayerKilled
            .filter(**filters, victim_id__not_isnull=True, weapon__in=internal_weapons)
            .group_by("weapon", "victim_id")
            .annotate(d_count=Count("id"))
            .values("weapon", "victim_id", "d_count")
        )

        for k in kills_data:
            wcode = k["weapon"]
            pid = k["attacker_id"]
            weapon_stats = stats_by_weapon.get(wcode)
            if weapon_stats is None:
                continue
            if pid not in weapon_stats:
                weapon_stats[pid] = {"kills": 0, "deaths": 0}
            weapon_stats[pid]["kills"] = k["k_count"]

        for d in deaths_data:
            wcode = d["weapon"]
            pid = d["victim_id"]
            weapon_stats = stats_by_weapon.get(wcode)
            if weapon_stats is None:
                continue
            if pid not in weapon_stats:
                weapon_stats[pid] = {"kills": 0, "deaths": 0}
            weapon_stats[pid]["deaths"] = d["d_count"]

        for iw in internal_weapons:
            weapon_stats = stats_by_weapon.get(iw, {})
            if not weapon_stats:
                continue
            best = None
            best_key: tuple[float, ...] | None = None
            for pid, data in weapon_stats.items():
                kills = data["kills"]
                deaths = data["deaths"]
                kd = calc_kd(kills, deaths)
                if kills < min_kills or deaths < min_deaths:
                    continue
                if sort == "kd":
                    key: tuple[float, ...] = (kd, kills)
                elif sort == "kills":
                    key = (kills,)
                else:
                    key = (deaths,)
                if best_key is None or key > best_key:
                    best = {"pid": pid, "kills": kills, "deaths": deaths, "kd": kd, "weapon": iw}
                    best_key = key
            if best:
                winners.append(best)
    if not winners:
        return paginated(data=[], total=0, msg=f"Weapon Leaderboard for {display_weapons_str} ({range})")
    player_ids = [w["pid"] for w in winners]
    players = await Player.filter(id__in=player_ids).values("id", "name", "nucleus_id")
    p_map = {p["id"]: p for p in players}
    results = []
    for w in winners:
        p_info = p_map.get(w["pid"])
        name = p_info["name"] if p_info else f"Unknown ({w['pid']})"
        nucleus_id = p_info["nucleus_id"] if p_info else None
        results.append({
            "weapon": to_display_weapon(w["weapon"]),
            "name": name,
            "nucleus_id": nucleus_id,
            "kills": w["kills"],
            "deaths": w["deaths"],
            "kd": w["kd"],
        })

    if sort == "kd":
        results.sort(key=lambda x: (x["kd"], x["kills"]), reverse=True)
    elif sort == "kills":
        results.sort(key=lambda x: x["kills"], reverse=True)
    elif sort == "deaths":
        results.sort(key=lambda x: x["deaths"], reverse=True)

    total = len(results)
    offset = (page_no - 1) * page_size
    paged_results = results[offset : offset + page_size]

    return paginated(
        data=paged_results,
        total=total,
        msg=f"Weapon Leaderboard for {display_weapons_str} ({range})",
    )


@router.get("/players/{nucleus_id_or_player_name}/vs_all")
async def get_player_vs_all_stats(
    nucleus_id_or_player_name: int | str,
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    sort: Literal["kills", "deaths", "kd"] = "kd",
):
    """获取特定玩家对其他所有人的 KD（从高到低）。"""

    player, err = await get_player_by_identifier(nucleus_id_or_player_name, require_nucleus_id=False)
    if err:
        return err
    assert player is not None

    pid = player.id

    kills_list = await PlayerKilled.filter(attacker_id=pid, victim_id__not_isnull=True).values("victim_id")
    deaths_list = await PlayerKilled.filter(victim_id=pid, attacker_id__not_isnull=True).values("attacker_id")

    opponents_stats = {}

    for k in kills_list:
        oid = k["victim_id"]
        if oid not in opponents_stats:
            opponents_stats[oid] = {"kills": 0, "deaths": 0}
        opponents_stats[oid]["kills"] += 1

    for d in deaths_list:
        oid = d["attacker_id"]
        if oid not in opponents_stats:
            opponents_stats[oid] = {"kills": 0, "deaths": 0}
        opponents_stats[oid]["deaths"] += 1

    if not opponents_stats:
        return success(data=[], msg=f"Player {nucleus_id_or_player_name} has no opponents")

    op_ids = list(opponents_stats.keys())
    ops = await Player.filter(id__in=op_ids).values("id", "name", "nucleus_id")
    op_map = {o["id"]: o for o in ops}

    results = []
    for oid, data in opponents_stats.items():
        op_info = op_map.get(oid)
        name = op_info["name"] if op_info else f"Unknown ({oid})"
        n_id = op_info["nucleus_id"] if op_info else None

        k = data["kills"]
        d = data["deaths"]
        kd = calc_kd(k, d)

        results.append({"opponent_name": name, "opponent_id": n_id, "kills": k, "deaths": d, "kd": kd})

    if sort == "kd":
        results.sort(key=lambda x: (x["kd"], x["kills"]), reverse=True)
    elif sort == "kills":
        results.sort(key=lambda x: (x["kills"], x["kd"]), reverse=True)
    elif sort == "deaths":
        results.sort(key=lambda x: (x["deaths"], x["kd"]), reverse=True)

    # Calculate Summary
    total_kills = len(kills_list)
    total_deaths = len(deaths_list)
    total_kd = calc_kd(total_kills, total_deaths)

    # Find Nemesis (宿敌) - High interaction, KD close to 1
    nemesis = None
    max_interaction = 0

    # Find Worst Enemy (天敌) - Highest Enemy KD (Deaths / Kills)
    worst_enemy = None

    for r in results:
        k = r["kills"]
        d = r["deaths"]
        if k == 0:
            r["enemy_kd"] = float(d) * 10000.0
            r["enemy_kd_display"] = float(d)
        else:
            r["enemy_kd"] = round(d / k, 2)
            r["enemy_kd_display"] = r["enemy_kd"]

    sorted_by_worst = sorted(results, key=lambda x: (x["enemy_kd"], x["deaths"]), reverse=True)

    candidates = [r for r in sorted_by_worst if r["deaths"] >= 5]
    if not candidates:
        candidates = [r for r in sorted_by_worst if r["deaths"] >= 2]
    if not candidates:
        candidates = sorted_by_worst

    if candidates:
        worst_enemy = candidates[0]

    for r in results:
        k = r["kills"]
        d = r["deaths"]
        kd = r["kd"]
        interaction = k + d

        if 0.6 <= kd <= 1.66:
            if interaction > max_interaction:
                max_interaction = interaction
                nemesis = r

    summary = {"total_kills": total_kills, "total_deaths": total_deaths, "kd": total_kd, "nemesis": nemesis, "worst_enemy": worst_enemy}

    player_info = {"name": player.name, "nucleus_id": player.nucleus_id, "country": player.country, "region": player.region}

    total = len(results)
    offset = (page_no - 1) * page_size
    paged_results = results[offset : offset + page_size]

    return paginated(data=paged_results, total=total, msg=f"KD Leaderboard for {nucleus_id_or_player_name}", summary=summary, player=player_info)


@router.get("/players/{nucleus_id_or_player_name}/weapons")
async def get_player_weapon_stats(
    nucleus_id_or_player_name: int | str,
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    sort: Literal["kills", "deaths", "kd"] = "kd",
):
    player, err = await get_player_by_identifier(nucleus_id_or_player_name, require_nucleus_id=False)
    if err:
        return err
    assert player is not None

    pid = player.id

    def normalize_weapon(w: str | None) -> str:
        s = (w or "").strip().lower()
        if not s:
            return s
        if s in WEAPON_NAME_MAP:
            return WEAPON_NAME_MAP[s]
        return s

    kills_list = await PlayerKilled.filter(attacker_id=pid, victim_id__not_isnull=True).values("weapon")
    deaths_list = await PlayerKilled.filter(victim_id=pid, attacker_id__not_isnull=True).values("weapon")

    weapon_stats = {}

    for k in kills_list:
        w = normalize_weapon(k.get("weapon"))
        if not w:
            continue
        if w not in weapon_stats:
            weapon_stats[w] = {"kills": 0, "deaths": 0}
        weapon_stats[w]["kills"] += 1

    for d in deaths_list:
        w = normalize_weapon(d.get("weapon"))
        if not w:
            continue
        if w not in weapon_stats:
            weapon_stats[w] = {"kills": 0, "deaths": 0}
        weapon_stats[w]["deaths"] += 1

    if not weapon_stats:
        return success(data=[], msg=f"Player {nucleus_id_or_player_name} has no weapon stats")

    results = []
    for w, data in weapon_stats.items():
        k = data["kills"]
        d = data["deaths"]
        kd = calc_kd(k, d)
        results.append({"weapon": WEAPON_NAME_MAP.get(w, w), "kills": k, "deaths": d, "kd": kd})

    if sort == "kd":
        results.sort(key=lambda x: (x["kd"], x["kills"]), reverse=True)
    elif sort == "kills":
        results.sort(key=lambda x: (x["kills"], x["kd"]), reverse=True)
    elif sort == "deaths":
        results.sort(key=lambda x: (x["deaths"], x["kd"]), reverse=True)

    total_kills = len(kills_list)
    total_deaths = len(deaths_list)
    total_kd = calc_kd(total_kills, total_deaths)

    player_info = {"name": player.name, "nucleus_id": player.nucleus_id, "country": player.country, "region": player.region}

    total = len(results)
    offset = (page_no - 1) * page_size
    paged_results = results[offset : offset + page_size]

    return paginated(
        data=paged_results,
        total=total,
        msg=f"Weapon stats for {nucleus_id_or_player_name}",
        summary={"total_kills": total_kills, "total_deaths": total_deaths, "kd": total_kd},
        player=player_info,
    )
