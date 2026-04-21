"""ws_service 终端输出层：rich 美化 + 事件聚合 + 比赛状态心跳。

设计：
- 里程碑事件（MatchSetup / MatchStateEnd / Connect / Disconnect / GameStateChanged / Init）
  → 直接用 console 打印，突出显示
- 爆发类事件（PlayerKilled / PlayerDowned / PlayerAssist / PlayerDamaged / PlayerRevive /
  InventoryPickUp/Drop/Use / GrenadeThrown / ZiplineUsed）
  → 进 EventAggregator 累计，10 秒窗口 flush 一次摘要（击杀会带 top actor）
- MatchTracker 记录当前 active 比赛，心跳 30s 打印一行持续时长 + 在线数
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from time import monotonic

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# 全局 rich Console —— 非 TTY 场景自动降级纯文本
console = Console()

# 爆发类事件聚合窗口（秒）
BURST_FLUSH_INTERVAL = 10
# 比赛状态心跳间隔（秒）
HEARTBEAT_INTERVAL = 30

# 爆发事件中文标签
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


class EventAggregator:
    """按类别累计爆发事件；flush 时一行摘要输出。"""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        # kind -> actor_name -> count
        self._actors: dict[str, Counter[str]] = defaultdict(Counter)

    def add(self, kind: str, actor: str | None = None) -> None:
        self._counts[kind] += 1
        if actor:
            self._actors[kind][actor] += 1

    def is_empty(self) -> bool:
        return not self._counts

    def flush(self) -> None:
        if self.is_empty():
            return
        total = sum(self._counts.values())
        parts: list[str] = []
        for kind, count in self._counts.most_common():
            label = _BURST_LABELS.get(kind, kind)
            top = self._actors.get(kind)
            if top:
                top_str = ", ".join(f"{name}×{cnt}" for name, cnt in top.most_common(3))
                parts.append(f"[bold]{count}[/bold] {label} ([dim]{top_str}[/dim])")
            else:
                parts.append(f"[bold]{count}[/bold] {label}")
        console.print(
            Text.from_markup(f"⚔️  [cyan]burst[/cyan] [{total} events]  ")
            + Text.from_markup("  |  ".join(parts))
        )
        self._counts.clear()
        self._actors.clear()


class MatchTracker:
    """跟踪当前 active 比赛（单 slot，适合单游戏服 ws_service）。"""

    def __init__(self) -> None:
        self.active: dict | None = None  # {server_id, map_name, playlist_name, playlist_desc, started_at, started_mono}
        # 在线玩家集合 —— 仅用于心跳展示
        self._online_players: set[str] = set()

    def on_match_setup(
        self,
        server_id: str,
        map_name: str,
        playlist_name: str,
        playlist_desc: str,
        started_ts: int,
    ) -> None:
        started_dt = datetime.fromtimestamp(started_ts, tz=timezone.utc)
        self.active = {
            "server_id": server_id,
            "map_name": map_name,
            "playlist_name": playlist_name,
            "playlist_desc": playlist_desc,
            "started_at": started_dt,
            "started_mono": monotonic(),
        }
        self._online_players.clear()

        tbl = Table.grid(padding=(0, 2))
        tbl.add_row("[dim]map[/dim]", f"[green]{map_name}[/green]")
        tbl.add_row(
            "[dim]playlist[/dim]",
            f"[green]{playlist_name}[/green] [dim]({playlist_desc})[/dim]",
        )
        tbl.add_row("[dim]server[/dim]", f"[yellow]{server_id}[/yellow]")
        tbl.add_row(
            "[dim]starts[/dim]",
            started_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        )
        console.print(
            Panel(
                tbl,
                title="[bold magenta]🎮 Match Start[/bold magenta]",
                border_style="magenta",
                expand=False,
            )
        )

    def on_match_end(self, winners: list[str] | None = None) -> None:
        if not self.active:
            logger.info("🏁 match end (no active match to close)")
            return
        dur = _fmt_duration(monotonic() - self.active["started_mono"])
        winner_str = ", ".join(winners) if winners else "-"
        console.rule(
            f"[bold cyan]🏁 Match End  winners={winner_str}  duration={dur}[/bold cyan]",
            style="cyan",
        )
        self.active = None
        self._online_players.clear()

    def on_player_connected(self, name: str) -> None:
        self._online_players.add(name)

    def on_player_disconnected(self, name: str) -> None:
        self._online_players.discard(name)

    def heartbeat(self) -> None:
        if not self.active:
            return
        dur = _fmt_duration(monotonic() - self.active["started_mono"])
        online = len(self._online_players)
        console.print(
            Text.from_markup(
                f"📊 [cyan]ACTIVE[/cyan] "
                f"[yellow]{self.active['playlist_name']}[/yellow]/"
                f"[green]{self.active['map_name']}[/green]  "
                f"elapsed=[bold]{dur}[/bold]  "
                f"online=[bold]{online}[/bold]  "
                f"server={self.active['server_id']}"
            )
        )


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h:d}h{m:02d}m{s:02d}s"
    return f"{m:d}m{s:02d}s"


# 全局单例
aggregator = EventAggregator()
match_tracker = MatchTracker()
