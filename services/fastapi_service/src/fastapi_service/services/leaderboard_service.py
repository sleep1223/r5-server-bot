from __future__ import annotations

from datetime import datetime

from shared_lib.config import settings
from shared_lib.models import Player, PlayerKilled, Server
from tortoise.expressions import F
from tortoise.functions import Count

from fastapi_service.core.constants import WEAPON_NAME_MAP, to_display_weapon, to_internal_weapon
from fastapi_service.core.utils import calc_kd, get_date_range

# ── Common helpers ──


async def _get_excluded_server_ids() -> list[int]:
    """解析 settings.kd_excluded_server_hosts 配置为服务器 id 列表，用于统计时排除。"""
    hosts = settings.kd_excluded_server_hosts
    if not hosts:
        return []
    rows = await Server.filter(host__in=hosts).values("id")
    return [r["id"] for r in rows]


def _sort_results(results: list[dict], sort: str, *, secondary_key: str = "kills") -> None:
    if sort == "kd":
        results.sort(key=lambda x: (x["kd"], x["kills"]), reverse=True)
    elif sort == "kills":
        results.sort(key=lambda x: (x["kills"], x.get("kd", 0)), reverse=True)
    elif sort == "deaths":
        results.sort(key=lambda x: (x["deaths"], x.get("kd", 0)), reverse=True)


def _paginate(results: list, *, offset: int, page_size: int) -> tuple[list, int]:
    total = len(results)
    return results[offset : offset + page_size], total


async def _aggregate_kills_deaths(
    start_time: datetime | None,
    end_time: datetime | None,
    *,
    extra_filters: dict | None = None,
    group_kills_by: str = "attacker_id",
    group_deaths_by: str = "victim_id",
    excluded_server_ids: list[int] | None = None,
) -> dict[int, dict[str, int]]:
    """统一的 kill/death 聚合，返回 {player_id: {"kills": N, "deaths": N}}"""
    filters: dict = {}
    if start_time:
        filters["created_at__gte"] = start_time
    if end_time:
        filters["created_at__lte"] = end_time
    if extra_filters:
        filters.update(extra_filters)
    if excluded_server_ids:
        filters["server_id__not_in"] = excluded_server_ids

    kills_filter = {**filters, f"{group_kills_by}__isnull": False}
    kills_data = (
        await PlayerKilled.filter(**kills_filter)
        .exclude(attacker_id=F("victim_id"))
        .group_by(group_kills_by)
        .annotate(k_count=Count("id"))
        .values(group_kills_by, "k_count")
    )

    deaths_filter = {**filters, f"{group_deaths_by}__isnull": False}
    deaths_data = (
        await PlayerKilled.filter(**deaths_filter)
        .exclude(attacker_id=F("victim_id"))
        .group_by(group_deaths_by)
        .annotate(d_count=Count("id"))
        .values(group_deaths_by, "d_count")
    )

    stats: dict[int, dict[str, int]] = {}
    for k in kills_data:
        pid = k[group_kills_by]
        stats.setdefault(pid, {"kills": 0, "deaths": 0})["kills"] = k["k_count"]
    for d in deaths_data:
        pid = d[group_deaths_by]
        stats.setdefault(pid, {"kills": 0, "deaths": 0})["deaths"] = d["d_count"]
    return stats


async def _enrich_with_player_names(stats: dict[int, dict], player_ids: list[int] | None = None) -> dict[int, dict]:
    """为 stats dict 中的 player_id 添加 name 和 nucleus_id"""
    ids = player_ids or list(stats.keys())
    players = await Player.filter(id__in=ids).values("id", "name", "nucleus_id", "status")
    return {p["id"]: p for p in players}


# ── KD Leaderboard ──


async def get_kd_ranking(
    *,
    range_type: str,
    sort: str,
    min_kills: int,
    min_deaths: int,
    offset: int,
    page_size: int,
    server_id: int | None = None,
) -> tuple[list[dict], int]:
    start_time, end_time = get_date_range(range_type)
    extra_filters = {"server_id": server_id} if server_id is not None else None
    # 显式指定 server_id 时不再应用全局排除，允许单独查看被排除服务器的数据
    excluded_ids = None if server_id is not None else await _get_excluded_server_ids()
    stats = await _aggregate_kills_deaths(start_time, end_time, extra_filters=extra_filters, excluded_server_ids=excluded_ids)

    if not stats:
        return [], 0

    p_map = await _enrich_with_player_names(stats)

    results = []
    for pid, data in stats.items():
        kills, deaths = data["kills"], data["deaths"]
        if kills < min_kills or deaths < min_deaths:
            continue
        p_info = p_map.get(pid)
        if p_info and p_info.get("status") == "banned":
            continue
        results.append({
            "name": p_info["name"] if p_info else f"Unknown ({pid})",
            "nucleus_id": p_info["nucleus_id"] if p_info else None,
            "kills": kills,
            "deaths": deaths,
            "kd": calc_kd(kills, deaths),
        })

    _sort_results(results, sort)
    return _paginate(results, offset=offset, page_size=page_size)


# ── Weapon Leaderboard ──


async def get_weapon_ranking(
    *,
    weapons: list[str],
    range_type: str,
    sort: str,
    min_kills: int,
    min_deaths: int,
    offset: int,
    page_size: int,
    server_id: int | None = None,
) -> tuple[list[dict], int, str]:
    internal_weapons = [to_internal_weapon(w) for w in weapons]
    internal_weapons = [w for w in internal_weapons if w]
    if not internal_weapons:
        return [], 0, "Invalid weapon(s)"

    display_weapons_str = ", ".join(to_display_weapon(w) for w in internal_weapons)

    start_time, end_time = get_date_range(range_type)
    filters: dict = {}
    if start_time and end_time:
        filters["created_at__range"] = (start_time, end_time)
    if server_id is not None:
        filters["server_id"] = server_id
    else:
        excluded_ids = await _get_excluded_server_ids()
        if excluded_ids:
            filters["server_id__not_in"] = excluded_ids

    # Aggregate by (weapon, player)
    stats_by_weapon: dict[str, dict[int, dict[str, int]]] = {iw: {} for iw in internal_weapons}

    kills_data = await (
        PlayerKilled
        .filter(**filters, attacker_id__not_isnull=True, weapon__in=internal_weapons)
        .exclude(attacker_id=F("victim_id"))
        .group_by("weapon", "attacker_id")
        .annotate(k_count=Count("id"))
        .values("weapon", "attacker_id", "k_count")
    )
    deaths_data = await (
        PlayerKilled.filter(**filters, victim_id__not_isnull=True, weapon__in=internal_weapons)
        .exclude(attacker_id=F("victim_id"))
        .group_by("weapon", "victim_id")
        .annotate(d_count=Count("id"))
        .values("weapon", "victim_id", "d_count")
    )

    for k in kills_data:
        weapon_stats = stats_by_weapon.get(k["weapon"])
        if weapon_stats is None:
            continue
        weapon_stats.setdefault(k["attacker_id"], {"kills": 0, "deaths": 0})["kills"] = k["k_count"]

    for d in deaths_data:
        weapon_stats = stats_by_weapon.get(d["weapon"])
        if weapon_stats is None:
            continue
        weapon_stats.setdefault(d["victim_id"], {"kills": 0, "deaths": 0})["deaths"] = d["d_count"]

    # Collect all player IDs involved, then exclude banned
    all_pids = {pid for ws in stats_by_weapon.values() for pid in ws}
    banned_ids = set()
    if all_pids:
        banned_players = await Player.filter(id__in=list(all_pids), status="banned").values_list("id", flat=True)
        banned_ids = set(banned_players)

    # Find best player per weapon
    winners = []
    for iw in internal_weapons:
        weapon_stats = stats_by_weapon.get(iw, {})
        if not weapon_stats:
            continue
        best = None
        best_key: tuple[float, ...] | None = None
        for pid, data in weapon_stats.items():
            kills, deaths = data["kills"], data["deaths"]
            kd = calc_kd(kills, deaths)
            if kills < min_kills or deaths < min_deaths:
                continue
            if pid in banned_ids:
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
        return [], 0, display_weapons_str

    player_ids = [w["pid"] for w in winners]
    p_map = await _enrich_with_player_names({}, player_ids)

    results = []
    for w in winners:
        p_info = p_map.get(w["pid"])
        results.append({
            "weapon": to_display_weapon(w["weapon"]),
            "name": p_info["name"] if p_info else f"Unknown ({w['pid']})",
            "nucleus_id": p_info["nucleus_id"] if p_info else None,
            "kills": w["kills"],
            "deaths": w["deaths"],
            "kd": w["kd"],
        })

    _sort_results(results, sort)
    paged, total = _paginate(results, offset=offset, page_size=page_size)
    return paged, total, display_weapons_str


# ── Player vs All ──


async def get_player_vs_all(
    *,
    player_id: int,
    sort: str,
    offset: int,
    page_size: int,
    server_id: int | None = None,
    range_type: str = "all",
) -> tuple[list[dict], int, dict]:
    server_filter: dict = {}
    if server_id is not None:
        server_filter["server_id"] = server_id
    else:
        excluded_ids = await _get_excluded_server_ids()
        if excluded_ids:
            server_filter["server_id__not_in"] = excluded_ids
    start_time, end_time = get_date_range(range_type)
    if start_time:
        server_filter["created_at__gte"] = start_time
    if end_time:
        server_filter["created_at__lte"] = end_time
    kills_list = (
        await PlayerKilled.filter(attacker_id=player_id, victim_id__not_isnull=True, **server_filter)
        .exclude(victim_id=player_id)
        .values("victim_id")
    )
    deaths_list = (
        await PlayerKilled.filter(victim_id=player_id, attacker_id__not_isnull=True, **server_filter)
        .exclude(attacker_id=player_id)
        .values("attacker_id")
    )

    opponents_stats: dict[int, dict[str, int]] = {}
    for k in kills_list:
        opponents_stats.setdefault(k["victim_id"], {"kills": 0, "deaths": 0})["kills"] += 1
    for d in deaths_list:
        opponents_stats.setdefault(d["attacker_id"], {"kills": 0, "deaths": 0})["deaths"] += 1

    if not opponents_stats:
        return [], 0, _build_vs_all_summary(0, 0, None, None)

    op_map = await _enrich_with_player_names(opponents_stats)

    results = []
    for oid, data in opponents_stats.items():
        op_info = op_map.get(oid)
        k, d = data["kills"], data["deaths"]
        results.append({
            "opponent_name": op_info["name"] if op_info else f"Unknown ({oid})",
            "opponent_id": op_info["nucleus_id"] if op_info else None,
            "kills": k,
            "deaths": d,
            "kd": calc_kd(k, d),
        })

    _sort_results(results, sort)

    # Calculate summary
    total_kills = len(kills_list)
    total_deaths = len(deaths_list)

    # Enemy KD for worst enemy detection
    for r in results:
        k, d = r["kills"], r["deaths"]
        if k == 0:
            r["enemy_kd"] = float(d) * 10000.0
            r["enemy_kd_display"] = float(d)
        else:
            r["enemy_kd"] = round(d / k, 2)
            r["enemy_kd_display"] = r["enemy_kd"]

    worst_enemy = _find_worst_enemy(results)
    nemesis = _find_nemesis(results)
    summary = _build_vs_all_summary(total_kills, total_deaths, nemesis, worst_enemy)

    paged, total = _paginate(results, offset=offset, page_size=page_size)
    return paged, total, summary


def _find_worst_enemy(results: list[dict]) -> dict | None:
    sorted_by_worst = sorted(results, key=lambda x: (x["enemy_kd"], x["deaths"]), reverse=True)
    candidates = [r for r in sorted_by_worst if r["deaths"] >= 5]
    if not candidates:
        candidates = [r for r in sorted_by_worst if r["deaths"] >= 2]
    if not candidates:
        candidates = sorted_by_worst
    return candidates[0] if candidates else None


def _find_nemesis(results: list[dict]) -> dict | None:
    nemesis = None
    max_interaction = 0
    for r in results:
        k, d, kd = r["kills"], r["deaths"], r["kd"]
        interaction = k + d
        if 0.6 <= kd <= 1.66 and interaction > max_interaction:
            max_interaction = interaction
            nemesis = r
    return nemesis


def _build_vs_all_summary(total_kills: int, total_deaths: int, nemesis: dict | None, worst_enemy: dict | None) -> dict:
    return {
        "total_kills": total_kills,
        "total_deaths": total_deaths,
        "kd": calc_kd(total_kills, total_deaths),
        "nemesis": nemesis,
        "worst_enemy": worst_enemy,
    }


# ── Player Weapon Stats ──


async def get_player_weapon_stats(
    *,
    player_id: int,
    sort: str,
    offset: int,
    page_size: int,
    server_id: int | None = None,
    range_type: str = "all",
) -> tuple[list[dict], int, dict]:
    server_filter: dict = {}
    if server_id is not None:
        server_filter["server_id"] = server_id
    else:
        excluded_ids = await _get_excluded_server_ids()
        if excluded_ids:
            server_filter["server_id__not_in"] = excluded_ids
    start_time, end_time = get_date_range(range_type)
    if start_time:
        server_filter["created_at__gte"] = start_time
    if end_time:
        server_filter["created_at__lte"] = end_time
    kills_list = (
        await PlayerKilled.filter(attacker_id=player_id, victim_id__not_isnull=True, **server_filter)
        .exclude(victim_id=player_id)
        .values("weapon")
    )
    deaths_list = (
        await PlayerKilled.filter(victim_id=player_id, attacker_id__not_isnull=True, **server_filter)
        .exclude(attacker_id=player_id)
        .values("weapon")
    )

    weapon_stats: dict[str, dict[str, int]] = {}

    for k in kills_list:
        w = _normalize_weapon(k.get("weapon"))
        if not w:
            continue
        weapon_stats.setdefault(w, {"kills": 0, "deaths": 0})["kills"] += 1

    for d in deaths_list:
        w = _normalize_weapon(d.get("weapon"))
        if not w:
            continue
        weapon_stats.setdefault(w, {"kills": 0, "deaths": 0})["deaths"] += 1

    if not weapon_stats:
        return [], 0, {"total_kills": 0, "total_deaths": 0, "kd": 0.0}

    results = []
    for w, data in weapon_stats.items():
        k, d = data["kills"], data["deaths"]
        results.append({"weapon": WEAPON_NAME_MAP.get(w, w), "kills": k, "deaths": d, "kd": calc_kd(k, d)})

    _sort_results(results, sort)

    total_kills = len(kills_list)
    total_deaths = len(deaths_list)
    summary = {"total_kills": total_kills, "total_deaths": total_deaths, "kd": calc_kd(total_kills, total_deaths)}

    paged, total = _paginate(results, offset=offset, page_size=page_size)
    return paged, total, summary


def _normalize_weapon(w: str | None) -> str:
    s = (w or "").strip().lower()
    if not s:
        return s
    if s in WEAPON_NAME_MAP:
        return WEAPON_NAME_MAP[s]
    return s
