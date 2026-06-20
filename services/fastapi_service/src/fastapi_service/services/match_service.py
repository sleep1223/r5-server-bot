from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from shared_lib.models import Match, Player, PlayerKilled, PlayerMatchWeaponStat, SdkMatchEndReport, Server
from tortoise import connections
from tortoise.expressions import Q
from tortoise.functions import Count

from fastapi_service.core.utils import calc_kd, get_date_range
from fastapi_service.services import player_access_service

# PlayerKilled 按 created_at 月度分区；按 match_id 聚合时用 match 时间推出
# created_at 边界，保证分区裁剪命中。两端留 buffer 覆盖事件处理抖动。
_PARTITION_PRUNE_LEAD = timedelta(hours=2)  # match started_at 之前写入的事件容差
_PARTITION_PRUNE_TAIL = timedelta(minutes=30)  # ended_at 之后仍可能写入的尾巴
_SDK_MATCH_END_CATEGORY = "sdk_match_end"


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


def _safe_int(value: object | None, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _safe_float(value: object | None, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _timestamp_to_dt(value: object) -> datetime:
    timestamp = _safe_int(value, 0)
    if timestamp <= 0:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _sdk_full_match_id(server: Server, ended_at: datetime) -> str:
    server_key = str(server.server_id or server.host or server.id).replace(":", "-")
    return f"{server_key}-{ended_at:%Y%m%d-%H%M%S}-sdk"


def _cache_player_aliases(
    player_map: dict[str, Player],
    player: Player,
    *,
    uid: object | None = None,
    nucleus_id: object | None = None,
) -> None:
    for value in (
        player_access_service.normalize_uid(uid, nucleus_id),
        player_access_service.normalize_uid(uid),
        player_access_service.normalize_uid(nucleus_id),
        player_access_service.normalize_uid(player.nucleus_id),
    ):
        if value:
            player_map[value] = player


def _lookup_report_player(
    player_map: dict[str, Player],
    *,
    uid: object | None = None,
    nucleus_id: object | None = None,
) -> Player | None:
    for value in (
        player_access_service.normalize_uid(uid, nucleus_id),
        player_access_service.normalize_uid(uid),
        player_access_service.normalize_uid(nucleus_id),
    ):
        if value and value in player_map:
            return player_map[value]
    return None


async def _upsert_report_players(players: list[dict[str, Any]]) -> tuple[int, dict[str, Player]]:
    count = 0
    player_map: dict[str, Player] = {}
    for player in players:
        uid = player.get("uid")
        nucleus_id = player.get("nucleusId")
        if not uid and nucleus_id is None:
            continue
        saved = await player_access_service.upsert_access_player_snapshot(
            uid=uid,
            nucleus_id=nucleus_id,
            player_name=player.get("playerName"),
        )
        if saved:
            count += 1
            _cache_player_aliases(player_map, saved, uid=uid, nucleus_id=nucleus_id)
    return count, player_map


async def _find_or_create_sdk_match(server: Server, report: dict[str, Any], ended_at: datetime) -> tuple[Match, bool]:
    map_name = str(report.get("map") or "") or "unknown"
    playlist_name = str(report.get("playlist") or "") or "unknown"
    active = await Match.filter(server=server, status="active").order_by("-started_at").first()
    updates = {
        "map_name": map_name,
        "playlist_name": playlist_name,
        "playlist_desc": playlist_name,
        "ended_at": ended_at,
        "status": "completed",
        "end_reason": "sdk_match_end",
        "current_state": "WinnerDetermined",
        "has_entered_playing": True,
    }
    if active:
        await Match.filter(id=active.id).update(**updates)
        for key, value in updates.items():
            setattr(active, key, value)
        return active, False

    full_match_id = _sdk_full_match_id(server, ended_at)
    existing = await Match.get_or_none(full_match_id=full_match_id)
    if existing:
        await Match.filter(id=existing.id).update(**updates)
        for key, value in updates.items():
            setattr(existing, key, value)
        return existing, False

    match = await Match.create(
        full_match_id=full_match_id,
        server=server,
        map_name=map_name,
        playlist_name=playlist_name,
        playlist_desc=playlist_name,
        datacenter=None,
        aim_assist_on=False,
        started_at=ended_at,
        ended_at=ended_at,
        status="completed",
        end_reason="sdk_match_end",
        current_state="WinnerDetermined",
        has_entered_playing=True,
    )
    return match, True


def _weapon_stats_from_payload(player_payload: dict[str, Any]) -> list[dict[str, Any]]:
    weapon_stats = [stat for stat in player_payload.get("weaponStats") or [] if isinstance(stat, dict)]
    if weapon_stats:
        return weapon_stats

    fallback_stats: list[dict[str, Any]] = []
    for weapon_kill in player_payload.get("weaponKills") or []:
        if not isinstance(weapon_kill, dict):
            continue
        fallback_stats.append(
            {
                "weapon": weapon_kill.get("weapon"),
                "kills": weapon_kill.get("kills"),
            }
        )
    return fallback_stats


async def _save_sdk_weapon_stats(
    *,
    match: Match,
    server: Server,
    players: list[dict[str, Any]],
    player_map: dict[str, Player],
) -> int:
    await PlayerMatchWeaponStat.filter(match=match, source=_SDK_MATCH_END_CATEGORY).delete()

    rows: list[PlayerMatchWeaponStat] = []
    for player_payload in players:
        player = _lookup_report_player(
            player_map,
            uid=player_payload.get("uid"),
            nucleus_id=player_payload.get("nucleusId"),
        )
        if player is None:
            continue

        for stat in _weapon_stats_from_payload(player_payload):
            weapon = str(stat.get("weapon") or "").strip() or "unknown"
            shots = max(_safe_int(stat.get("shots"), 0), 0)
            hits = max(_safe_int(stat.get("hits"), 0), 0)
            bullets_hit = max(_safe_float(stat.get("bulletsHit"), 0.0), 0.0)
            damage = max(_safe_float(stat.get("damage"), 0.0), 0.0)
            headshots = max(_safe_int(stat.get("headshots"), 0), 0)
            kills = max(_safe_int(stat.get("kills"), 0), 0)

            accuracy = _safe_float(stat.get("accuracy"), -1.0)
            if accuracy < 0.0:
                accuracy = float(hits) / float(shots) if shots > 0 else 0.0
            accuracy_percent = _safe_float(stat.get("accuracyPercent"), -1.0)
            if accuracy_percent < 0.0:
                accuracy_percent = accuracy * 100.0

            rows.append(
                PlayerMatchWeaponStat(
                    player=player,
                    match=match,
                    server=server,
                    weapon=weapon,
                    shots=shots,
                    hits=hits,
                    bullets_hit=bullets_hit,
                    damage=damage,
                    headshots=headshots,
                    kills=kills,
                    accuracy=accuracy,
                    accuracy_percent=accuracy_percent,
                    source=_SDK_MATCH_END_CATEGORY,
                )
            )

    if rows:
        await PlayerMatchWeaponStat.bulk_create(rows)
    return len(rows)


async def process_match_end_report(report: dict[str, Any]) -> dict[str, Any]:
    server_identifier = str(report.get("serverId") or "").strip()
    ended_at = _timestamp_to_dt(report.get("endedAt"))
    players = [player for player in report.get("players") or [] if isinstance(player, dict)]

    server = await player_access_service.upsert_sdk_server_snapshot(
        server_id=server_identifier,
        server_ip=report.get("serverIp"),
        server_port=report.get("serverPort"),
        map_name=report.get("map"),
        num_players=report.get("numPlayers"),
        max_players=report.get("maxPlayers"),
        has_status=True,
    )
    if server is None:
        raise ValueError("serverIp/serverPort 无效，无法创建或关联服务器")

    player_count, player_map = await _upsert_report_players(players)
    kill_events = [event for event in report.get("killEvents") or [] if isinstance(event, dict)]
    match, created_match = await _find_or_create_sdk_match(server, report, ended_at)

    report_values = {
        "server_id": server.id,
        "match_id": match.id,
        "server_identifier": server_identifier,
        "server_ip": str(report.get("serverIp") or "") or None,
        "server_port": _safe_int(report.get("serverPort"), 0) or None,
        "map_name": str(report.get("map") or "") or None,
        "playlist_name": str(report.get("playlist") or "") or None,
        "sdk_version": str(report.get("sdkVersion") or "") or None,
        "tick": _safe_int(report.get("tick"), 0) or None,
        "spawn_count": _safe_int(report.get("spawnCount"), 0),
        "ended_at": ended_at,
        "num_players": _safe_int(report.get("numPlayers"), len(players)),
        "max_players": _safe_int(report.get("maxPlayers"), 0),
        "payload": report,
    }
    existing_report = await SdkMatchEndReport.filter(
        server_identifier=server_identifier,
        ended_at=ended_at,
        tick=report_values["tick"],
    ).first()
    if existing_report:
        await SdkMatchEndReport.filter(id=existing_report.id).update(**report_values)
        report_id = existing_report.id
    else:
        saved_report = await SdkMatchEndReport.create(**report_values)
        report_id = saved_report.id

    weapon_stat_count = await _save_sdk_weapon_stats(
        match=match,
        server=server,
        players=players,
        player_map=player_map,
    )

    logger.info(
        "对局结束上报: "
        f"server_id={server_identifier or server.id}, report_id={report_id}, "
        f"match_id={match.id}, players={player_count}/{len(players)}, "
        f"kill_events={len(kill_events)}, weapon_stats={weapon_stat_count}, "
        f"created_match={created_match}, map={report_values['map_name'] or '-'}, "
        f"playlist={report_values['playlist_name'] or '-'}"
    )
    return {
        "report_id": report_id,
        "server_id": server.id,
        "match_id": match.id,
        "created_match": created_match,
        "players": player_count,
        "kill_events": len(kill_events),
        "weapon_stats": weapon_stat_count,
    }


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
    matches = (
        await match_query
        .order_by("-ended_at")
        .limit(fetch_n)
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

    match_ids = [m["id"] for m in matches]
    pk_bounds = _pk_created_at_bounds(matches)

    # 批量聚合击杀 / 死亡。单 SQL 搞定所有 match。
    kill_rows = (
        await PlayerKilled
        .filter(match_id__in=match_ids, attacker_id__isnull=False, **pk_bounds)
        .group_by("match_id", "attacker_id")
        .annotate(k_count=Count("id"))
        .values("match_id", "attacker_id", "k_count")
    )
    death_rows = (
        await PlayerKilled
        .filter(match_id__in=match_ids, victim_id__isnull=False, **pk_bounds)
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
        results.append({
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
        })
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
        servers = await Server.filter(id__in=list(involved_server_ids)).values("id", "host", "name", "short_name")
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
        await PlayerKilled
        .filter(
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
        await Match
        .filter(id__in=match_ids, status="completed")
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
    kill_rows = await PlayerKilled.filter(match_id__in=scoped_match_ids, attacker_id=player_id, **pk_bounds).group_by("match_id").annotate(k_count=Count("id")).values("match_id", "k_count")
    death_rows = await PlayerKilled.filter(match_id__in=scoped_match_ids, victim_id=player_id, **pk_bounds).group_by("match_id").annotate(d_count=Count("id")).values("match_id", "d_count")
    kills_by_match = {r["match_id"]: r["k_count"] for r in kill_rows}
    deaths_by_match = {r["match_id"]: r["d_count"] for r in death_rows}

    # 4) 富化 server
    involved_server_ids = {m["server_id"] for m in matches}
    s_map: dict[int, dict] = {}
    if involved_server_ids:
        servers = await Server.filter(id__in=list(involved_server_ids)).values("id", "host", "name", "short_name")
        s_map = {s["id"]: s for s in servers}

    # 5) 装配 + 排序
    results: list[dict] = []
    for m in matches:
        k = kills_by_match.get(m["id"], 0)
        d = deaths_by_match.get(m["id"], 0)
        results.append({
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
        })

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
    players = await Player.filter(id__in=player_ids).values("id", "name", "nucleus_id", "status")
    p_map = {p["id"]: p for p in players}

    results: list[dict] = []
    for r in rows:
        pid = r["attacker_id"]
        p_info = p_map.get(pid)
        if p_info and p_info.get("status") == "banned":
            continue
        results.append({
            "name": p_info["name"] if p_info else f"Unknown ({pid})",
            "nucleus_id": p_info["nucleus_id"] if p_info else None,
            "total_kills": r["total_kills"],
            "counted_matches": r["counted_matches"],
        })

    return results, total_players
