from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger
from shared_lib.database import close_db, init_db
from shared_lib.models import Match, PlayerMatchWeaponStat, SdkMatchEndReport, Server
from tortoise.transactions import in_transaction

from fastapi_service.services import match_service
from fastapi_service.tasks.refresh_player_kill_daily_stats import refresh_player_kill_daily_stats_window

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_SDK_MATCH_END_CATEGORY = "sdk_match_end"


@dataclass
class RebuildSummary:
    reports_seen: int = 0
    reports_rebuilt: int = 0
    reports_skipped: int = 0
    reports_failed: int = 0
    old_weapon_rows: int = 0
    new_weapon_rows: int = 0
    kill_events_seen: int = 0
    kill_events_saved: int = 0


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _to_shanghai_date(value: datetime) -> date:
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(_SHANGHAI_TZ).date()


def _shanghai_day_start_utc(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=_SHANGHAI_TZ).astimezone(timezone.utc)


def _payload_dict(report: SdkMatchEndReport) -> dict | None:
    return report.payload if isinstance(report.payload, dict) else None


async def _resolve_server_and_match(report: SdkMatchEndReport) -> tuple[Server | None, Match | None]:
    server = getattr(report, "server", None)
    match = getattr(report, "match", None)
    server_id = getattr(report, "server_id", None)
    match_id = getattr(report, "match_id", None)

    if server is None and server_id:
        server = await Server.get_or_none(id=server_id)
    if match is None and match_id:
        match = await Match.get_or_none(id=match_id)
    if server is None and match is not None and getattr(match, "server_id", None):
        server = await Server.get_or_none(id=match.server_id)

    return server, match


def _report_queryset(
    *,
    start_day: date | None,
    end_day: date | None,
    report_ids: list[int] | None,
    server_id: int | None,
    match_id: int | None,
):
    query = SdkMatchEndReport.all()
    if start_day is not None:
        query = query.filter(ended_at__gte=_shanghai_day_start_utc(start_day))
    if end_day is not None:
        query = query.filter(ended_at__lt=_shanghai_day_start_utc(end_day))
    if report_ids:
        query = query.filter(id__in=report_ids)
    if server_id is not None:
        query = query.filter(server_id=server_id)
    if match_id is not None:
        query = query.filter(match_id=match_id)
    return query


async def _rebuild_one_report(
    report: SdkMatchEndReport,
    *,
    apply: bool,
) -> tuple[bool, int, int, int, int, date | None]:
    payload = _payload_dict(report)
    if payload is None:
        logger.warning(f"跳过 sdk_match_end_report id={report.id}: payload 不是 JSON object")
        return False, 0, 0, 0, 0, None

    server, match = await _resolve_server_and_match(report)
    if server is None or match is None:
        logger.warning(f"跳过 sdk_match_end_report id={report.id}: 缺少 server/match 关联(server_id={getattr(report, 'server_id', None)}, match_id={getattr(report, 'match_id', None)})")
        return False, 0, 0, 0, 0, None

    players = [player for player in payload.get("players") or [] if isinstance(player, dict)]
    kill_events = [event for event in payload.get("killEvents") or [] if isinstance(event, dict)]
    old_rows = await PlayerMatchWeaponStat.filter(match=match, source=_SDK_MATCH_END_CATEGORY).count()

    if not apply:
        logger.info(f"dry-run sdk_match_end_report id={report.id}: match_id={match.id}, old_weapon_rows={old_rows}, players={len(players)}, kill_events={len(kill_events)}")
        return False, old_rows, 0, len(kill_events), 0, _to_shanghai_date(report.ended_at)

    async with in_transaction():
        _player_count, player_map = await match_service._upsert_report_players(players)  # noqa: SLF001
        new_rows, saved_kill_events = await match_service._save_sdk_weapon_stats(  # noqa: SLF001
            match=match,
            server=server,
            players=players,
            kill_events=kill_events,
            player_map=player_map,
        )

    logger.info(
        f"已重建 sdk_match_end_report id={report.id}: "
        f"match_id={match.id}, old_weapon_rows={old_rows}, new_weapon_rows={new_rows}, "
        f"kill_events={saved_kill_events}/{len(kill_events)}"
    )
    return True, old_rows, new_rows, len(kill_events), saved_kill_events, _to_shanghai_date(report.ended_at)


async def rebuild_sdk_match_weapon_stats(
    *,
    apply: bool = False,
    start_day: date | None = None,
    end_day: date | None = None,
    report_ids: list[int] | None = None,
    server_id: int | None = None,
    match_id: int | None = None,
    batch_size: int = 100,
    refresh_daily_stats: bool = False,
    stop_on_error: bool = False,
) -> RebuildSummary:
    summary = RebuildSummary()
    last_id = 0
    affected_start_day: date | None = None
    affected_end_day: date | None = None

    while True:
        reports = await (
            _report_queryset(
                start_day=start_day,
                end_day=end_day,
                report_ids=report_ids,
                server_id=server_id,
                match_id=match_id,
            )
            .filter(id__gt=last_id)
            .select_related("server", "match")
            .order_by("id")
            .limit(max(1, batch_size))
        )
        if not reports:
            break

        for report in reports:
            last_id = report.id
            summary.reports_seen += 1
            try:
                did_rebuild, old_rows, new_rows, kill_events, saved_kill_events, affected_day = await _rebuild_one_report(
                    report,
                    apply=apply,
                )
            except Exception:
                summary.reports_failed += 1
                logger.exception(f"重建 sdk_match_end_report id={report.id} 失败")
                if stop_on_error:
                    raise
                continue

            summary.old_weapon_rows += old_rows
            summary.new_weapon_rows += new_rows
            summary.kill_events_seen += kill_events
            summary.kill_events_saved += saved_kill_events
            if did_rebuild:
                summary.reports_rebuilt += 1
            else:
                summary.reports_skipped += 1

            if affected_day is not None:
                affected_start_day = affected_day if affected_start_day is None else min(affected_start_day, affected_day)
                affected_end_day = affected_day + timedelta(days=1) if affected_end_day is None else max(affected_end_day, affected_day + timedelta(days=1))

    if apply and refresh_daily_stats and affected_start_day is not None and affected_end_day is not None:
        logger.info(f"刷新 player_kill_daily_weapon_opponent_stats: {affected_start_day}..{affected_end_day}")
        await refresh_player_kill_daily_stats_window(affected_start_day, affected_end_day)

    return summary


async def _run_from_args(args: argparse.Namespace) -> RebuildSummary:
    await init_db(generate_schemas=False)
    try:
        return await rebuild_sdk_match_weapon_stats(
            apply=args.apply,
            start_day=args.start,
            end_day=args.end,
            report_ids=args.report_id,
            server_id=args.server_id,
            match_id=args.match_id,
            batch_size=args.batch_size,
            refresh_daily_stats=args.refresh_daily_stats,
            stop_on_error=args.stop_on_error,
        )
    finally:
        await close_db()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Rebuild player_match_weapon_stats rows from sdk_match_end_reports.payload. Default mode is dry-run; pass --apply to overwrite current sdk_match_end rows.")
    )
    parser.add_argument("--apply", action="store_true", help="Overwrite player_match_weapon_stats for matched reports.")
    parser.add_argument("--start", type=_parse_day, help="Report ended_at start date, inclusive, YYYY-MM-DD.")
    parser.add_argument("--end", type=_parse_day, help="Report ended_at end date, exclusive, YYYY-MM-DD.")
    parser.add_argument("--report-id", type=int, action="append", help="Only rebuild this sdk_match_end_reports.id. Can be passed multiple times.")
    parser.add_argument("--server-id", type=int, help="Only rebuild reports for this Server.id.")
    parser.add_argument("--match-id", type=int, help="Only rebuild reports for this Match.id.")
    parser.add_argument("--batch-size", type=int, default=100, help="Reports to scan per batch.")
    parser.add_argument("--refresh-daily-stats", action="store_true", help="Refresh player_kill_daily_weapon_opponent_stats for affected report dates after --apply.")
    parser.add_argument("--stop-on-error", action="store_true", help="Abort on the first failed report.")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    if not args.apply:
        logger.warning("dry-run 模式：不会覆盖数据。确认范围后添加 --apply 才会写库。")
    summary = asyncio.run(_run_from_args(args))
    logger.info(f"rebuild summary: {summary}")


if __name__ == "__main__":
    main()
