"""ws_service 终端仪表盘：rich.Live 驱动的 5 段式画面。

画面分块（render() 返回 Layout）：
  1. header      —— 服务元信息（uptime / listen / public / ingest / clients）
  2. match       —— 当前对局详情（playlist/map/server/时间/duration/state/online/ring）
  3. burst       —— 爆发事件 10s 窗口累计 + 本局累计
  4. ingest      —— 上报队列状态 / 下次批次 / 最近一次结果 / 会话统计
  5. milestones  —— 最近 N 条里程碑事件滚动条

非 TTY 环境（systemd/nohup/重定向）自动降级为纯文本单次渲染。
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

BURST_FLUSH_INTERVAL = 10
HEARTBEAT_INTERVAL = 30
MILESTONE_MAX = 10
PLAYER_LIST_PREVIEW = 5
DASHBOARD_REFRESH_PER_SEC = 4

_BURST_LABELS: dict[str, str] = {
    "kill": "击杀",
    "downed": "倒地",
    "assist": "助攻",
    "damaged": "伤害",
    "revive": "救起",
    "pickup": "拾取",
    "drop": "丢弃",
    "use": "使用",
    "grenade": "手雷",
    "zipline": "滑索",
}


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h:d}h{m:02d}m{s:02d}s"
    return f"{m:d}m{s:02d}s"


def _fmt_hms(seconds: float) -> str:
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class EventAggregator:
    """10s 窗口爆发事件累计；另外保留本局累计（跨窗口）。"""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._actors: dict[str, Counter[str]] = defaultdict(Counter)
        self._match_totals: Counter[str] = Counter()

    def add(self, kind: str, actor: str | None = None) -> None:
        self._counts[kind] += 1
        self._match_totals[kind] += 1
        if actor:
            self._actors[kind][actor] += 1

    def window_total(self) -> int:
        return sum(self._counts.values())

    def match_total(self) -> int:
        return sum(self._match_totals.values())

    def snapshot(self) -> list[tuple[str, int, list[tuple[str, int]]]]:
        rows: list[tuple[str, int, list[tuple[str, int]]]] = []
        for kind, count in self._counts.most_common():
            top = self._actors.get(kind)
            rows.append((kind, count, top.most_common(3) if top else []))
        return rows

    def reset_window(self) -> None:
        self._counts.clear()
        self._actors.clear()

    def reset_match(self) -> None:
        self._match_totals.clear()


@dataclass
class MatchState:
    server_id: str
    map_name: str
    playlist_name: str
    playlist_desc: str
    aim_assist_on: bool
    started_at: datetime
    started_mono: float
    state: str = ""  # 最近一次 GameStateChanged
    online_players: set[str] = field(default_factory=set)
    ring_stage: int = 0
    ring_shrink_duration: int = 0
    squads_eliminated: int = 0


class MatchTracker:
    def __init__(self) -> None:
        self.active: MatchState | None = None

    def on_match_setup(
        self,
        server_id: str,
        map_name: str,
        playlist_name: str,
        playlist_desc: str,
        aim_assist_on: bool,
        started_ts: int,
    ) -> None:
        self.active = MatchState(
            server_id=server_id,
            map_name=map_name,
            playlist_name=playlist_name,
            playlist_desc=playlist_desc,
            aim_assist_on=aim_assist_on,
            started_at=datetime.fromtimestamp(started_ts, tz=timezone.utc),
            started_mono=monotonic(),
        )

    def on_match_end(self) -> None:
        self.active = None

    def on_state_changed(self, state: str) -> None:
        if self.active:
            self.active.state = state

    def on_player_connected(self, name: str) -> None:
        if self.active and name:
            self.active.online_players.add(name)

    def on_player_disconnected(self, name: str) -> None:
        if self.active and name:
            self.active.online_players.discard(name)

    def on_ring_start_closing(self, stage: int, shrink_duration: int) -> None:
        if self.active:
            self.active.ring_stage = stage
            self.active.ring_shrink_duration = shrink_duration

    def on_squad_eliminated(self) -> None:
        if self.active:
            self.active.squads_eliminated += 1


@dataclass
class IngestStats:
    buffer_len: int = 0
    buffer_max: int = 0
    next_batch_at: float = 0.0  # monotonic 时间戳
    last_ok: bool | None = None
    last_ts: datetime | None = None
    last_batch_id: str = ""
    last_count: int = 0
    last_latency_ms: int = 0
    batches_ok: int = 0
    batches_fail: int = 0
    events_uploaded: int = 0


@dataclass
class ServiceInfo:
    listen_host: str = ""
    listen_port: int = 0
    public_host: str = ""
    public_port: int = 0
    ingest_url: str = ""
    ws_clients: int = 0
    started_mono: float = field(default_factory=monotonic)


@dataclass
class Milestone:
    ts: datetime
    icon: str
    kind: str
    detail: str


class DashboardState:
    def __init__(self) -> None:
        self.service = ServiceInfo()
        self.match = MatchTracker()
        self.aggregator = EventAggregator()
        self.ingest = IngestStats()
        self.milestones: deque[Milestone] = deque(maxlen=MILESTONE_MAX)

    def push_milestone(self, icon: str, kind: str, detail: str) -> None:
        self.milestones.append(
            Milestone(
                ts=datetime.now().astimezone(),
                icon=icon,
                kind=kind,
                detail=detail,
            )
        )

    # -- 渲染 ---------------------------------------------------------------

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._render_header(), name="header", size=4),
            Layout(self._render_match(), name="match", size=9),
            Layout(self._render_burst(), name="burst", size=12),
            Layout(self._render_ingest(), name="ingest", size=5),
            Layout(self._render_milestones(), name="milestones", minimum_size=8),
        )
        return layout

    def _render_header(self) -> Panel:
        uptime = _fmt_hms(monotonic() - self.service.started_mono)
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        svc = self.service

        tbl = Table.grid(padding=(0, 2), expand=True)
        tbl.add_column(ratio=1)
        tbl.add_column(ratio=1)
        tbl.add_row(
            Text.from_markup(f"[dim]listen[/dim]  ws://{svc.listen_host}:{svc.listen_port}  [dim]clients[/dim] [bold]{svc.ws_clients}[/bold]"),
            Text.from_markup(f"[dim]public[/dim]  [yellow]{svc.public_host}:{svc.public_port}[/yellow]"),
        )
        tbl.add_row(
            Text.from_markup(f"[dim]ingest →[/dim] {svc.ingest_url}"),
            Text.from_markup(f"[dim]now[/dim] {now}"),
        )
        return Panel(
            tbl,
            title=f"[bold]R5 WS Service[/bold]  [dim]uptime[/dim] [cyan]{uptime}[/cyan]",
            border_style="blue",
        )

    def _render_match(self) -> Panel:
        m = self.match.active
        if not m:
            return Panel(
                Text.from_markup("[dim]等待下一场对局 (MatchSetup) ...[/dim]"),
                title="[bold magenta]🎮 Active Match[/bold magenta]",
                border_style="magenta",
            )

        duration = _fmt_duration(monotonic() - m.started_mono)
        started_local = m.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        state_color = {
            "Playing": "green",
            "Running": "green",
            "Prematch": "yellow",
            "Waiting": "yellow",
            "WaitingForPlayers": "yellow",
            "Postmatch": "red",
        }.get(m.state, "cyan")

        players = sorted(m.online_players)
        preview = players[:PLAYER_LIST_PREVIEW]
        rest = len(players) - len(preview)
        players_str = " · ".join(preview) if preview else "[dim]—[/dim]"
        if rest > 0:
            players_str += f"  [dim](+{rest})[/dim]"

        ring_str = f"stage [bold]{m.ring_stage}[/bold] shrink {m.ring_shrink_duration}s" if m.ring_stage else "[dim]—[/dim]"

        tbl = Table.grid(padding=(0, 2), expand=True)
        tbl.add_column(style="dim", width=11)
        tbl.add_column(ratio=1)
        tbl.add_column(style="dim", width=12)
        tbl.add_column(ratio=1)

        tbl.add_row(
            "playlist",
            Text.from_markup(f"[green]{m.playlist_name}[/green]  [dim]({m.playlist_desc})[/dim]"),
            "aim assist",
            Text.from_markup("[green]ON[/green]" if m.aim_assist_on else "[red]OFF[/red]"),
        )
        tbl.add_row(
            "map",
            Text.from_markup(f"[green]{m.map_name}[/green]"),
            "server id",
            Text.from_markup(f"[yellow]{m.server_id}[/yellow]"),
        )
        tbl.add_row(
            "started",
            started_local,
            "duration",
            Text.from_markup(f"[bold]{duration}[/bold]"),
        )
        tbl.add_row(
            "online",
            Text.from_markup(f"[bold]{len(players)}[/bold]"),
            "squads out",
            Text.from_markup(f"[bold]{m.squads_eliminated}[/bold]"),
        )
        tbl.add_row("ring", Text.from_markup(ring_str), "", "")
        tbl.add_row("players", Text.from_markup(players_str), "", "")

        state_text = m.state or "-"
        return Panel(
            tbl,
            title=(f"[bold magenta]🎮 Active Match[/bold magenta]   state: [bold {state_color}]{state_text}[/bold {state_color}]"),
            border_style="magenta",
        )

    def _render_burst(self) -> Panel:
        agg = self.aggregator
        rows = agg.snapshot()
        window_total = agg.window_total()
        match_total = agg.match_total()

        # 距离下次 flush 秒数
        remain = BURST_FLUSH_INTERVAL - ((monotonic() - self.service.started_mono) % BURST_FLUSH_INTERVAL)

        tbl = Table.grid(padding=(0, 2), expand=True)
        tbl.add_column(justify="right", width=5)
        tbl.add_column(width=8)
        tbl.add_column(ratio=1)

        if not rows:
            tbl.add_row("", Text.from_markup("[dim]—[/dim]"), "")
        else:
            for kind, count, top in rows:
                label = _BURST_LABELS.get(kind, kind)
                top_str = "  ".join(f"[dim]{n}[/dim]×[bold]{c}[/bold]" for n, c in top) if top else ""
                tbl.add_row(
                    Text.from_markup(f"[bold cyan]{count}[/bold cyan]"),
                    label,
                    Text.from_markup(top_str),
                )

        title = f"[bold]⚔  Burst window[/bold]   flush in [cyan]{remain:4.1f}s[/cyan] / {BURST_FLUSH_INTERVAL}s   window [bold]{window_total}[/bold]   match [bold]{match_total}[/bold]"
        return Panel(tbl, title=title, border_style="cyan")

    def _render_ingest(self) -> Panel:
        i = self.ingest
        now = monotonic()
        remain = max(0.0, i.next_batch_at - now) if i.next_batch_at else 0.0

        if i.last_ok is None:
            last_line = Text.from_markup("[dim]—  尚未上报[/dim]")
        else:
            mark = "[green]✅[/green]" if i.last_ok else "[red]❌[/red]"
            ts_str = i.last_ts.astimezone().strftime("%H:%M:%S") if i.last_ts else "—"
            last_line = Text.from_markup(f"{mark} [dim]last[/dim] {ts_str}  batch [cyan]{i.last_batch_id[:8]}…[/cyan]  count [bold]{i.last_count}[/bold]  latency [bold]{i.last_latency_ms}ms[/bold]")

        session_line = Text.from_markup(f"[dim]session[/dim] batches [green]{i.batches_ok}[/green] ok / [red]{i.batches_fail}[/red] fail   events uploaded [bold]{i.events_uploaded}[/bold]")

        title = f"[bold]📤 Ingest[/bold]   next batch in [cyan]{remain:4.1f}s[/cyan]   buffer [bold]{i.buffer_len}[/bold] / {i.buffer_max}"
        return Panel(Group(last_line, session_line), title=title, border_style="green")

    def _render_milestones(self) -> Panel:
        if not self.milestones:
            body: Any = Text.from_markup("[dim]（尚无里程碑事件）[/dim]")
        else:
            tbl = Table.grid(padding=(0, 2), expand=True)
            tbl.add_column(width=8, style="dim")
            tbl.add_column(width=3)
            tbl.add_column(width=22, style="bold")
            tbl.add_column(ratio=1)
            for m in reversed(self.milestones):
                tbl.add_row(
                    m.ts.strftime("%H:%M:%S"),
                    m.icon,
                    m.kind,
                    m.detail,
                )
            body = tbl
        return Panel(
            body,
            title=f"[bold]📜 Recent milestones[/bold]   [dim]({len(self.milestones)}/{MILESTONE_MAX})[/dim]",
            border_style="yellow",
        )


# 全局单例 —— listener / runner / loguru sink 共享
state = DashboardState()
aggregator = state.aggregator
match_tracker = state.match
