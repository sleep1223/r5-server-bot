from __future__ import annotations

from datetime import timedelta

from shared_lib.models import Match, Player, PlayerKilled, Server
from tortoise import connections
from tortoise.expressions import Q
from tortoise.functions import Count

from fastapi_service.core.utils import calc_kd, get_date_range

# PlayerKilled 按 created_at 月度分区；按 match_id 聚合时用 match 时间推出
# created_at 边界，保证分区裁剪命中。两端留 buffer 覆盖事件处理抖动。
_PARTITION_PRUNE_LEAD = timedelta(hours=2)  # match started_at 之前写入的事件容差
_PARTITION_PRUNE_TAIL = timedelta(minutes=30)  # ended_at 之后仍可能写入的尾巴


def _pk_created_at_bounds(matches: list[dict]) -> dict:
    """根据一批 match 的 started_at/ended_at 推出 PlayerKilled.created_at 过滤范围。

    目的：让 PG 走分区裁剪。matches 为空或缺时间时返回空 dict（退化为不加约束）。
    """
    started = [m["started_at"] for m in matches if m.get("started_at")]
    ended = [m["ended_at"] for m in matches if m.get("ended_at")]
    if not started:
        return {}
    bounds: dict = {"created_at__gte": min(started) - _PARTITION_PRUNE_LEAD}
    if ended:
        bounds["created_at__lte"] = max(ended) + _PARTITION_PRUNE_TAIL
    return bounds


async def get_recent_matches(
    *,
    limit: int,
    min_top_kills: int,
    server_id: int | None = None,
) -> list[dict]:
    """最近已完成对局（每场附带 top1 attacker）。

    - 过滤条件：top1.kills >= min_top_kills（否则该场不展示）
    - 排序：按 ended_at DESC
    - 返回裸 list（不分页），调用方直接展示
    """
    if limit <= 0:
        return []

    # 过预筛一批 match 做"找 top1"，因为部分场次 top1 击杀可能不达阈值会被丢。
    # 3× 够绝大多数正常场景；不足时就尽量返回能拿到的。
    fetch_n = max(limit * 3, limit + 5)

    match_query = Match.filter(status="completed")
    if server_id is not None:
        match_query = match_query.filter(server_id=server_id)
    matches = await match_query.order_by("-ended_at").limit(fetch_n).values(
        "id",
        "full_match_id",
        "server_id",
        "map_name",
        "playlist_name",
        "playlist_desc",
        "started_at",
        "ended_at",
    )
    if not matches:
        return []

    match_ids = [m["id"] for m in matches]
    pk_bounds = _pk_created_at_bounds(matches)

    # 批量聚合击杀 / 死亡。单 SQL 搞定所有 match。
    kill_rows = (
        await PlayerKilled.filter(match_id__in=match_ids, attacker_id__isnull=False, **pk_bounds)
        .group_by("match_id", "attacker_id")
        .annotate(k_count=Count("id"))
        .values("match_id", "attacker_id", "k_count")
    )
    death_rows = (
        await PlayerKilled.filter(match_id__in=match_ids, victim_id__isnull=False, **pk_bounds)
        .group_by("match_id", "victim_id")
        .annotate(d_count=Count("id"))
        .values("match_id", "victim_id", "d_count")
    )

    # match_id -> attacker_id -> kills
    kills_by_match: dict[int, dict[int, int]] = {}
    for row in kill_rows:
        kills_by_match.setdefault(row["match_id"], {})[row["attacker_id"]] = row["k_count"]
    # match_id -> victim_id -> deaths（用于 top 玩家的 deaths 数据）
    deaths_by_match: dict[int, dict[int, int]] = {}
    for row in death_rows:
        deaths_by_match.setdefault(row["match_id"], {})[row["victim_id"]] = row["d_count"]

    # 每场选 top1（击杀最多，同值时按更少死亡 tiebreak）
    results: list[dict] = []
    top_player_ids: set[int] = set()
    involved_server_ids: set[int] = set()
    for m in matches:
        attacker_kills = kills_by_match.get(m["id"]) or {}
        if not attacker_kills:
            continue
        # deaths 取该场 victim 聚合；attacker 没死过就为 0
        deaths_map = deaths_by_match.get(m["id"]) or {}
        top_pid, top_kills = max(
            attacker_kills.items(),
            key=lambda kv: (kv[1], -deaths_map.get(kv[0], 0)),
        )
        if top_kills < min_top_kills:
            continue
        top_deaths = deaths_map.get(top_pid, 0)
        results.append(
            {
                "match_id": m["id"],
                "full_match_id": m["full_match_id"],
                "map_name": m["map_name"],
                "playlist_name": m["playlist_name"],
                "playlist_desc": m["playlist_desc"],
                "started_at": m["started_at"].isoformat() if m["started_at"] else None,
                "ended_at": m["ended_at"].isoformat() if m["ended_at"] else None,
                "server_id": m["server_id"],
                "top": {
                    "player_id": top_pid,
                    "kills": top_kills,
                    "deaths": top_deaths,
                    "kd": calc_kd(top_kills, top_deaths),
                },
            }
        )
        top_player_ids.add(top_pid)
        involved_server_ids.add(m["server_id"])
        if len(results) >= limit:
            break

    # 富化 top1 玩家名 + server 信息
    if top_player_ids:
        players = await Player.filter(id__in=list(top_player_ids)).values("id", "name", "nucleus_id")
        p_map = {p["id"]: p for p in players}
        for r in results:
            pid = r["top"]["player_id"]
            p_info = p_map.get(pid)
            r["top"]["name"] = p_info["name"] if p_info else f"Unknown ({pid})"
            r["top"]["nucleus_id"] = p_info["nucleus_id"] if p_info else None

    if involved_server_ids:
        servers = await Server.filter(id__in=list(involved_server_ids)).values(
            "id", "host", "name", "short_name"
        )
        s_map = {s["id"]: s for s in servers}
        for r in results:
            r["server"] = s_map.get(r["server_id"]) or {"id": r["server_id"]}

    return results


async def get_player_matches(
    *,
    player_id: int,
    limit: int,
    sort: str = "time",
    server_id: int | None = None,
) -> list[dict]:
    """该玩家最近参与过的 N 场已完成对局（含本场 kills / deaths / KD）。

    - "最近"：按 match.ended_at DESC 选 N 场（只看 completed）
    - sort 仅影响**展示顺序**：time(默认) / kills / kd
    - 只要玩家在本场有 kill 或 death 即视为参与
    """
    if limit <= 0:
        return []

    # 1) 玩家参与过的所有 match_id（用现有 attacker_id / victim_id 单列索引高效去重）
    participation_filters: dict = {"match_id__isnull": False}
    if server_id is not None:
        participation_filters["server_id"] = server_id

    match_id_rows = (
        await PlayerKilled.filter(
            Q(attacker_id=player_id) | Q(victim_id=player_id),
            **participation_filters,
        )
        .distinct()
        .values_list("match_id", flat=True)
    )
    if not match_id_rows:
        return []
    match_ids = [mid for mid in match_id_rows if mid is not None]
    if not match_ids:
        return []

    # 2) 拿最近 N 场 completed match 元数据
    matches = (
        await Match.filter(id__in=match_ids, status="completed")
        .order_by("-ended_at")
        .limit(limit)
        .values(
            "id",
            "full_match_id",
            "server_id",
            "map_name",
            "playlist_name",
            "playlist_desc",
            "started_at",
            "ended_at",
        )
    )
    if not matches:
        return []

    scoped_match_ids = [m["id"] for m in matches]
    pk_bounds = _pk_created_at_bounds(matches)

    # 3) 批量聚合本人在这些场次的击杀 / 死亡
    kill_rows = (
        await PlayerKilled.filter(match_id__in=scoped_match_ids, attacker_id=player_id, **pk_bounds)
        .group_by("match_id")
        .annotate(k_count=Count("id"))
        .values("match_id", "k_count")
    )
    death_rows = (
        await PlayerKilled.filter(match_id__in=scoped_match_ids, victim_id=player_id, **pk_bounds)
        .group_by("match_id")
        .annotate(d_count=Count("id"))
        .values("match_id", "d_count")
    )
    kills_by_match = {r["match_id"]: r["k_count"] for r in kill_rows}
    deaths_by_match = {r["match_id"]: r["d_count"] for r in death_rows}

    # 4) 富化 server
    involved_server_ids = {m["server_id"] for m in matches}
    s_map: dict[int, dict] = {}
    if involved_server_ids:
        servers = await Server.filter(id__in=list(involved_server_ids)).values(
            "id", "host", "name", "short_name"
        )
        s_map = {s["id"]: s for s in servers}

    # 5) 装配 + 排序
    results: list[dict] = []
    for m in matches:
        k = kills_by_match.get(m["id"], 0)
        d = deaths_by_match.get(m["id"], 0)
        results.append(
            {
                "match_id": m["id"],
                "full_match_id": m["full_match_id"],
                "map_name": m["map_name"],
                "playlist_name": m["playlist_name"],
                "playlist_desc": m["playlist_desc"],
                "started_at": m["started_at"].isoformat() if m["started_at"] else None,
                "ended_at": m["ended_at"].isoformat() if m["ended_at"] else None,
                "server_id": m["server_id"],
                "server": s_map.get(m["server_id"]) or {"id": m["server_id"]},
                "kills": k,
                "deaths": d,
                "kd": calc_kd(k, d),
            }
        )

    if sort == "kills":
        results.sort(key=lambda x: (x["kills"], x["kd"]), reverse=True)
    elif sort == "kd":
        results.sort(key=lambda x: (x["kd"], x["kills"]), reverse=True)
    # sort == "time" 已由 DB 排序完成

    return results


async def get_competitive_ranking(
    *,
    range_type: str,
    limit: int,
    offset: int,
    top_per_day: int,
    server_id: int | None = None,
) -> tuple[list[dict], int]:
    """竞技榜：每人每天取击杀数前 N 场，把这些场次击杀求和，按总和排序。

    - range_type: today / week(this_week) / last_week
    - 当日：等价于每人当天最多 N 场的击杀总和
    - 周榜：整周跨天求和，每日仍各取前 N 场
    - 依赖 PG 窗口函数（ROW_NUMBER OVER PARTITION BY）
    """
    start_time, end_time = get_date_range(range_type)
    if start_time is None or end_time is None:
        return [], 0

    conn = connections.get("default")

    # $1, $2 时间边界；$3 top_per_day；可选 $4 server_id；$N+1 limit；$N+2 offset
    params: list = [start_time, end_time, top_per_day]
    server_clause = ""
    if server_id is not None:
        params.append(server_id)
        server_clause = f"AND m.server_id = ${len(params)}"

    limit_idx = len(params) + 1
    offset_idx = len(params) + 2
    page_params = [*params, limit, offset]

    # pk.created_at 边界用于 PG 分区裁剪：match 可持续数小时，留 2h lead / 30min tail
    # 覆盖首杀早于 ended_at 窗口和末尾事件晚于 ended_at 的情形
    sql = f"""
    WITH player_match_kills AS (
        SELECT pk.attacker_id,
               pk.match_id,
               (m.started_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
               COUNT(*) AS kills
        FROM player_killed pk
        JOIN matches m ON pk.match_id = m.id
        WHERE m.ended_at BETWEEN $1 AND $2
          AND pk.created_at BETWEEN $1 - interval '2 hours' AND $2 + interval '30 minutes'
          AND m.status = 'completed'
          AND pk.attacker_id IS NOT NULL
          {server_clause}
        GROUP BY pk.attacker_id, pk.match_id, day
    ),
    ranked AS (
        SELECT attacker_id,
               match_id,
               day,
               kills,
               ROW_NUMBER() OVER (PARTITION BY attacker_id, day ORDER BY kills DESC) AS rn
        FROM player_match_kills
    ),
    agg AS (
        SELECT attacker_id,
               SUM(kills)::int AS total_kills,
               COUNT(*)::int AS counted_matches
        FROM ranked
        WHERE rn <= $3
        GROUP BY attacker_id
    )
    SELECT attacker_id,
           total_kills,
           counted_matches,
           COUNT(*) OVER ()::int AS total_players
    FROM agg
    ORDER BY total_kills DESC
    LIMIT ${limit_idx} OFFSET ${offset_idx}
    """

    rows = await conn.execute_query_dict(sql, page_params)

    if not rows:
        return [], 0

    total_players = rows[0].get("total_players", 0)

    # 富化 + 剔除 banned（跟 leaderboard_service 风格一致）
    player_ids = [r["attacker_id"] for r in rows]
    players = await Player.filter(id__in=player_ids).values(
        "id", "name", "nucleus_id", "status"
    )
    p_map = {p["id"]: p for p in players}

    results: list[dict] = []
    for r in rows:
        pid = r["attacker_id"]
        p_info = p_map.get(pid)
        if p_info and p_info.get("status") == "banned":
            continue
        results.append(
            {
                "name": p_info["name"] if p_info else f"Unknown ({pid})",
                "nucleus_id": p_info["nucleus_id"] if p_info else None,
                "total_kills": r["total_kills"],
                "counted_matches": r["counted_matches"],
            }
        )

    return results, total_players
