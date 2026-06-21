from __future__ import annotations

from datetime import date, datetime, timedelta

from shared_lib.config import settings
from shared_lib.models import Player, Server
from tortoise import connections

from fastapi_service.core.constants import WEAPON_NAME_MAP, to_display_weapon, to_internal_weapon
from fastapi_service.core.utils import CN_TZ, calc_kd, get_date_range

# ── Common helpers ──

_DAILY_STATS_TABLE = "player_kill_daily_weapon_opponent_stats"


def _normalize_input_device(value: str | None) -> str | None:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return None
    if text in {"controller", "gamepad", "pad", "joystick", "xinput"}:
        return "controller"
    if text in {"keyboard_mouse", "keyboard", "mouse", "kbm", "mnk", "keyboardmouse", "keyboard_and_mouse"}:
        return "keyboard_mouse"
    if text in {"unknown", "none", "null"}:
        return "unknown"
    return text[:50]


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
    if group_kills_by != "attacker_id" or group_deaths_by != "victim_id":
        return {}

    if extra_filters is not None and set(extra_filters) != {"server_id"}:
        return {}

    start_day, end_day = _stat_date_window(start_time, end_time)
    server_id = extra_filters.get("server_id") if extra_filters else None
    return await _aggregate_kills_deaths_from_daily_stats(
        start_day,
        end_day,
        server_id=server_id,
        excluded_server_ids=excluded_server_ids,
    )


def _stat_date_window(start_time: datetime | None, end_time: datetime | None) -> tuple[date | None, date | None]:
    start_day = start_time.astimezone(CN_TZ).date() if start_time else None
    end_day = end_time.astimezone(CN_TZ).date() + timedelta(days=1) if end_time else None
    return start_day, end_day


def _append_param(params: list[object], value: object) -> str:
    params.append(value)
    return f"${len(params)}"


def _extend_where_sql(where_sql: str, clause: str) -> str:
    if where_sql:
        return f"{where_sql}\n      AND {clause}"
    return f"WHERE {clause}"


def _daily_stats_filter_sql(
    *,
    start_day: date | None,
    end_day: date | None,
    server_id: int | None = None,
    input_device: str | None = None,
    excluded_server_ids: list[int] | None = None,
    alias: str = "s",
) -> tuple[str, list[object]]:
    params: list[object] = []
    prefix = f"{alias}." if alias else ""
    clauses: list[str] = []

    if start_day is not None:
        clauses.append(f"{prefix}stat_date >= {_append_param(params, start_day)}::date")
    if end_day is not None:
        clauses.append(f"{prefix}stat_date < {_append_param(params, end_day)}::date")
    if server_id is not None:
        clauses.append(f"{prefix}server_id = {_append_param(params, server_id)}")
    elif excluded_server_ids:
        clauses.append(f"{prefix}server_id <> ALL({_append_param(params, excluded_server_ids)}::int[])")
    normalized_input_device = _normalize_input_device(input_device)
    if normalized_input_device is not None:
        clauses.append(f"{prefix}input_device = {_append_param(params, normalized_input_device)}")

    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


async def _aggregate_kills_deaths_from_daily_stats(
    start_day: date | None,
    end_day: date | None,
    *,
    server_id: int | None = None,
    input_device: str | None = None,
    excluded_server_ids: list[int] | None = None,
) -> dict[int, dict[str, int]]:
    where_sql, params = _daily_stats_filter_sql(
        start_day=start_day,
        end_day=end_day,
        server_id=server_id,
        input_device=input_device,
        excluded_server_ids=excluded_server_ids,
    )

    sql = f"""
    SELECT
        player_id,
        SUM(kills)::int AS kills,
        SUM(deaths)::int AS deaths
    FROM {_DAILY_STATS_TABLE} s
    {where_sql}
    GROUP BY player_id
    HAVING SUM(kills) > 0 OR SUM(deaths) > 0
    """

    rows = await connections.get("default").execute_query_dict(sql, params)
    return {row["player_id"]: {"kills": row["kills"] or 0, "deaths": row["deaths"] or 0} for row in rows if row["player_id"] is not None}


async def _enrich_with_player_names(stats: dict[int, dict], player_ids: list[int] | None = None) -> dict[int, dict]:
    """为 stats dict 中的 player_id 添加 name 和 nucleus_id"""
    ids = player_ids or list(stats.keys())
    players = await Player.filter(id__in=ids).values("id", "name", "nucleus_id", "status", "input_device")
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
            "input_device": (p_info or {}).get("input_device") or "unknown",
            "kills": kills,
            "deaths": deaths,
            "kd": calc_kd(kills, deaths),
        })

    _sort_results(results, sort)
    return _paginate(results, offset=offset, page_size=page_size)


async def get_input_device_kill_ranking(
    *,
    range_type: str,
    sort: str,
    min_kills: int,
    min_deaths: int,
    offset: int,
    page_size: int,
    input_device: str | None = None,
    server_id: int | None = None,
) -> tuple[list[dict], int]:
    start_time, end_time = get_date_range(range_type)
    start_day, end_day = _stat_date_window(start_time, end_time)
    excluded_ids = None if server_id is not None else await _get_excluded_server_ids()
    where_sql, params = _daily_stats_filter_sql(
        start_day=start_day,
        end_day=end_day,
        server_id=server_id,
        input_device=input_device,
        excluded_server_ids=excluded_ids,
    )

    rows = await connections.get("default").execute_query_dict(
        f"""
        SELECT
            s.input_device,
            s.player_id,
            SUM(s.kills)::int AS kills,
            SUM(s.deaths)::int AS deaths
        FROM {_DAILY_STATS_TABLE} s
        {where_sql}
        GROUP BY s.input_device, s.player_id
        HAVING SUM(s.kills) > 0 OR SUM(s.deaths) > 0
        """,
        params,
    )

    if not rows:
        return [], 0

    player_ids = [row["player_id"] for row in rows if row["player_id"] is not None]
    p_map = await _enrich_with_player_names({}, player_ids)

    results = []
    for row in rows:
        pid = row["player_id"]
        if pid is None:
            continue
        kills, deaths = row["kills"] or 0, row["deaths"] or 0
        if kills < min_kills or deaths < min_deaths:
            continue
        p_info = p_map.get(pid)
        if p_info and p_info.get("status") == "banned":
            continue
        results.append({
            "name": p_info["name"] if p_info else f"Unknown ({pid})",
            "nucleus_id": p_info["nucleus_id"] if p_info else None,
            "input_device": row.get("input_device") or "unknown",
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
        return [], 0, "武器参数无效"

    display_weapons_str = ", ".join(to_display_weapon(w) for w in internal_weapons)

    start_time, end_time = get_date_range(range_type)
    start_day, end_day = _stat_date_window(start_time, end_time)
    excluded_ids = None if server_id is not None else await _get_excluded_server_ids()
    where_sql, params = _daily_stats_filter_sql(
        start_day=start_day,
        end_day=end_day,
        server_id=server_id,
        excluded_server_ids=excluded_ids,
    )
    where_sql = _extend_where_sql(where_sql, f"s.weapon = ANY({_append_param(params, internal_weapons)}::text[])")

    # Aggregate by (weapon, player, input device)
    stats_by_weapon: dict[str, dict[tuple[int, str], dict[str, int]]] = {iw: {} for iw in internal_weapons}

    rows = await connections.get("default").execute_query_dict(
        f"""
        SELECT
            s.weapon,
            s.player_id,
            s.input_device,
            SUM(s.kills)::int AS kills,
            SUM(s.deaths)::int AS deaths
        FROM {_DAILY_STATS_TABLE} s
        {where_sql}
        GROUP BY s.weapon, s.player_id, s.input_device
        HAVING SUM(s.kills) > 0 OR SUM(s.deaths) > 0
        """,
        params,
    )

    for row in rows:
        weapon_stats = stats_by_weapon.get(row["weapon"])
        if weapon_stats is None:
            continue
        weapon_stats[(row["player_id"], row.get("input_device") or "unknown")] = {"kills": row["kills"] or 0, "deaths": row["deaths"] or 0}

    # Collect all player IDs involved, then exclude banned
    all_pids = {pid for ws in stats_by_weapon.values() for pid, _device in ws}
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
        for (pid, input_device), data in weapon_stats.items():
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
                best = {"pid": pid, "input_device": input_device, "kills": kills, "deaths": deaths, "kd": kd, "weapon": iw}
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
            "input_device": w["input_device"],
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
    start_time, end_time = get_date_range(range_type)
    start_day, end_day = _stat_date_window(start_time, end_time)
    excluded_ids = None if server_id is not None else await _get_excluded_server_ids()
    where_sql, params = _daily_stats_filter_sql(
        start_day=start_day,
        end_day=end_day,
        server_id=server_id,
        excluded_server_ids=excluded_ids,
    )
    where_sql = _extend_where_sql(where_sql, f"s.player_id = {_append_param(params, player_id)}")

    total_rows = await connections.get("default").execute_query_dict(
        f"""
        SELECT
            COALESCE(SUM(s.kills), 0)::int AS kills,
            COALESCE(SUM(s.deaths), 0)::int AS deaths
        FROM {_DAILY_STATS_TABLE} s
        {where_sql}
        """,
        params,
    )
    total_row = total_rows[0] if total_rows else {"kills": 0, "deaths": 0}
    total_kills = total_row["kills"] or 0
    total_deaths = total_row["deaths"] or 0
    opponent_where_sql = _extend_where_sql(where_sql, "s.opponent_id IS NOT NULL")

    rows = await connections.get("default").execute_query_dict(
        f"""
        SELECT
            s.opponent_id,
            SUM(s.kills)::int AS kills,
            SUM(s.deaths)::int AS deaths
        FROM {_DAILY_STATS_TABLE} s
        {opponent_where_sql}
        GROUP BY s.opponent_id
        HAVING SUM(s.kills) > 0 OR SUM(s.deaths) > 0
        """,
        params,
    )

    opponents_stats: dict[int, dict[str, int]] = {}
    for row in rows:
        opponents_stats[row["opponent_id"]] = {"kills": row["kills"] or 0, "deaths": row["deaths"] or 0}

    if not opponents_stats:
        return [], 0, _build_vs_all_summary(total_kills, total_deaths, None, None)

    op_map = await _enrich_with_player_names(opponents_stats)

    results = []
    for oid, data in opponents_stats.items():
        op_info = op_map.get(oid)
        k, d = data["kills"], data["deaths"]
        results.append({
            "opponent_name": op_info["name"] if op_info else f"Unknown ({oid})",
            "opponent_id": op_info["nucleus_id"] if op_info else None,
            "input_device": (op_info or {}).get("input_device") or "unknown",
            "kills": k,
            "deaths": d,
            "kd": calc_kd(k, d),
        })

    _sort_results(results, sort)

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
    start_time, end_time = get_date_range(range_type)
    start_day, end_day = _stat_date_window(start_time, end_time)
    excluded_ids = None if server_id is not None else await _get_excluded_server_ids()
    where_sql, params = _daily_stats_filter_sql(
        start_day=start_day,
        end_day=end_day,
        server_id=server_id,
        excluded_server_ids=excluded_ids,
    )
    where_sql = _extend_where_sql(where_sql, f"s.player_id = {_append_param(params, player_id)}")

    rows = await connections.get("default").execute_query_dict(
        f"""
        SELECT
            s.weapon,
            s.input_device,
            SUM(s.kills)::int AS kills,
            SUM(s.deaths)::int AS deaths
        FROM {_DAILY_STATS_TABLE} s
        {where_sql}
        GROUP BY s.weapon, s.input_device
        HAVING SUM(s.kills) > 0 OR SUM(s.deaths) > 0
        """,
        params,
    )

    weapon_stats: dict[tuple[str, str], dict[str, int]] = {}

    for row in rows:
        w = _normalize_weapon(row.get("weapon"))
        if not w:
            continue
        input_device = row.get("input_device") or "unknown"
        weapon_stats[(w, input_device)] = {"kills": row["kills"] or 0, "deaths": row["deaths"] or 0}

    if not weapon_stats:
        return [], 0, {"total_kills": 0, "total_deaths": 0, "kd": 0.0}

    results = []
    for (w, input_device), data in weapon_stats.items():
        k, d = data["kills"], data["deaths"]
        results.append({"weapon": WEAPON_NAME_MAP.get(w, w), "input_device": input_device, "kills": k, "deaths": d, "kd": calc_kd(k, d)})

    _sort_results(results, sort)

    total_kills = sum(data["kills"] for data in weapon_stats.values())
    total_deaths = sum(data["deaths"] for data in weapon_stats.values())
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
