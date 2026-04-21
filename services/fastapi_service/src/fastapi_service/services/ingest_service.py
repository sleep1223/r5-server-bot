import asyncio
from collections import OrderedDict
from datetime import datetime, timezone

from loguru import logger
from shared_lib.models import (
    CharacterSelected,
    GameStateChanged,
    InitEvent,
    Match,
    MatchSetup,
    Player,
    PlayerConnected,
    PlayerDisconnected,
    PlayerKilled,
    Server,
)
from shared_lib.schemas.ingest import (
    CharacterSelectedIn,
    GameStateChangedIn,
    IngestBatch,
    IngestResult,
    InitEventIn,
    MatchSetupIn,
    PlayerConnectedIn,
    PlayerDisconnectedIn,
    PlayerInfo,
    PlayerKilledIn,
    ServerRef,
)
from tortoise.exceptions import IntegrityError

_SEEN_BATCH_IDS: "OrderedDict[str, None]" = OrderedDict()
_SEEN_BATCH_CAP = 2048

# 批次级锁：ingest 进程内序列化，避免同一玩家在并发批次下出现读改写竞态
# (尤其是 PlayerDisconnected 的 online_at -> total_playtime_seconds 累加
# 以及 Match 状态机的 read-modify-write)。ws_service 30s 一次 flush，
# 竞争极低，串行开销可忽略。
_BATCH_LOCK = asyncio.Lock()

# 每台服务器当前活跃对局缓存：server_id -> match_id。
# 重启后由 _restore_active_matches() 从 DB 恢复；进程内变更直接落库再更新缓存。
# 外部（close_stale_matches_task）标记 abandoned 时也要 pop 此处。
_ACTIVE_MATCH_BY_SERVER: dict[int, int] = {}

# GameStateChanged 中表示"正式开打"的状态值
_STATE_PLAYING = "Playing"
# 新一轮 Prematch 出现时，若上一局已进过 Playing 则视为对局结束
_STATE_PREMATCH = "Prematch"


async def restore_active_matches() -> None:
    """启动时调用：把 DB 里 status='active' 的 match 恢复到内存缓存。

    解决进程重启导致 _ACTIVE_MATCH_BY_SERVER 丢失后，事件被错误地绑到 NULL match 的问题。
    """
    _ACTIVE_MATCH_BY_SERVER.clear()
    actives = await Match.filter(status="active").all()
    for m in actives:
        _ACTIVE_MATCH_BY_SERVER[m.server_id] = m.id
    if actives:
        logger.info(f"Match 恢复活跃缓存: {len(actives)} 条 (server_id → match_id)")


def _mark_seen(batch_id: str) -> bool:
    """返回 True 表示此前见过该 batch_id（重复上报）。"""
    if batch_id in _SEEN_BATCH_IDS:
        _SEEN_BATCH_IDS.move_to_end(batch_id)
        return True
    _SEEN_BATCH_IDS[batch_id] = None
    if len(_SEEN_BATCH_IDS) > _SEEN_BATCH_CAP:
        _SEEN_BATCH_IDS.popitem(last=False)
    return False


async def _resolve_server(ref: ServerRef) -> Server:
    defaults = {
        "port": ref.port,
        "name": ref.name or f"server-{ref.host}",
        "is_self_hosted": True,
    }
    server, created = await Server.get_or_create(host=ref.host, defaults=defaults)
    if not server.is_self_hosted:
        server.is_self_hosted = True
        await server.save(update_fields=["is_self_hosted", "updated_at"])
        logger.info(f"Server {ref.host} 补标 is_self_hosted=True")
    if created:
        logger.info(f"Server 创建: id={server.id}, host={ref.host}")
    return server


def _collect_players(batch: IngestBatch) -> dict[str, PlayerInfo]:
    """聚合批次内涉及的所有唯一玩家。后出现的 PlayerInfo 覆盖同 hash 的早版本。"""
    found: dict[str, PlayerInfo] = {}

    def add(info: PlayerInfo | None) -> None:
        if not info or not info.nucleus_hash:
            return
        existing = found.get(info.nucleus_hash)
        # ip 字段"有值覆盖无值"，避免无 ip 的事件清空已有 ip
        resolved = info.model_copy(update={"ip": existing.ip}) if existing and not info.ip and existing.ip else info
        found[info.nucleus_hash] = resolved

    for event in batch.events:
        if isinstance(event, (CharacterSelectedIn, PlayerConnectedIn, PlayerDisconnectedIn)):
            add(event.player)
        elif isinstance(event, PlayerKilledIn):
            add(event.attacker)
            add(event.victim)
            add(event.awarded_to)
    return found


async def _bulk_upsert_players(infos: dict[str, PlayerInfo]) -> dict[str, Player]:
    """原子 upsert 所有涉及的玩家，返回 nucleus_hash -> Player 映射。

    用 Tortoise 的 ``bulk_create(on_conflict=..., update_fields=...)``
    → 翻译为 ``INSERT ... ON CONFLICT (nucleus_hash) DO UPDATE``，
    单条 SQL 完成所有 upsert，消除 ``update_or_create`` 的 check-then-insert 竞态。
    """
    if not infos:
        return {}

    # 按 ip 是否存在分两批：没有 ip 的事件不应抹掉已存在的 ip 列。
    with_ip: list[Player] = []
    without_ip: list[Player] = []
    for info in infos.values():
        obj = Player(
            nucleus_hash=info.nucleus_hash,
            name=info.name,
            hardware_name=info.hardware_name,
            ip=info.ip,
        )
        (with_ip if info.ip else without_ip).append(obj)

    if with_ip:
        await Player.bulk_create(
            with_ip,
            on_conflict=["nucleus_hash"],
            update_fields=["name", "hardware_name", "ip", "updated_at"],
        )
    if without_ip:
        await Player.bulk_create(
            without_ip,
            on_conflict=["nucleus_hash"],
            update_fields=["name", "hardware_name", "updated_at"],
        )

    rows = await Player.filter(nucleus_hash__in=list(infos.keys()))
    return {p.nucleus_hash: p for p in rows if p.nucleus_hash}


async def _apply_player_connected(player: Player) -> None:
    # 只改 status / online_at；使用条件 UPDATE 避免全字段 save() 的竞态
    await Player.filter(id=player.id).update(
        status="online",
        online_at=datetime.now(timezone.utc),
    )
    player.status = "online"  # 同步内存副本，供事件记录 FK 使用


async def _apply_player_disconnected(player: Player) -> tuple[int, str]:
    """断线：累加本段会话时长并切 offline。返回 (新 playtime, 新 status)。

    使用 ``filter(...).update(...)`` 一条 UPDATE 完成,避免读后写竞态。
    对 banned/kicked 玩家跳过状态变更以保留管理操作留下的状态。
    """
    # 读 online_at 做 session 计算 —— 放在锁内，单进程下不会被其他事件抢先改
    fresh = await Player.get_or_none(id=player.id)
    if not fresh:
        return player.total_playtime_seconds, player.status

    if fresh.status in ("banned", "kicked"):
        return fresh.total_playtime_seconds, fresh.status

    session_seconds = 0
    if fresh.online_at:
        session_seconds = max(0, int((datetime.now(timezone.utc) - fresh.online_at).total_seconds()))

    new_playtime = fresh.total_playtime_seconds + session_seconds
    await Player.filter(id=fresh.id).update(
        status="offline",
        total_playtime_seconds=new_playtime,
    )
    return new_playtime, "offline"


# ---------- Match 状态机 ----------


async def _load_active_match(server_id: int) -> Match | None:
    """取当前活跃对局。缓存 miss 时回源 DB，顺便修正缓存。"""
    cached = _ACTIVE_MATCH_BY_SERVER.get(server_id)
    if cached:
        m = await Match.get_or_none(id=cached)
        if m and m.status == "active":
            return m
        # 缓存陈旧：DB 里已非 active，清缓存
        _ACTIVE_MATCH_BY_SERVER.pop(server_id, None)
    # 回源：DB 里可能还有其它 active（理论上应至多 1 条）
    m = await Match.filter(server_id=server_id, status="active").order_by("-started_at").first()
    if m:
        _ACTIVE_MATCH_BY_SERVER[server_id] = m.id
    return m


async def _close_match(match_id: int, server_id: int, ts_ms: int, reason: str) -> None:
    """关闭对局：只有仍在 active 状态时 UPDATE 才生效（CAS）。"""
    ended_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    rows = await Match.filter(id=match_id, status="active").update(
        status="completed",
        ended_at=ended_at,
        end_reason=reason,
    )
    _ACTIVE_MATCH_BY_SERVER.pop(server_id, None)
    if rows:
        logger.info(f"Match 关闭: id={match_id}, reason={reason}")


async def _create_match(server: Server, ts_ms: int, event: MatchSetupIn) -> Match:
    """根据 MatchSetup 新建 Match；full_match_id 冲突时追加序号重试。"""
    started_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    base_id = f"{server.host}-{started_at:%Y%m%d-%H%M%S}"
    for attempt in range(5):
        candidate = base_id if attempt == 0 else f"{base_id}-{attempt}"
        try:
            match = await Match.create(
                full_match_id=candidate,
                server=server,
                map_name=event.map_name,
                playlist_name=event.playlist_name,
                playlist_desc=event.playlist_desc,
                datacenter=event.datacenter,
                aim_assist_on=event.aim_assist_on,
                started_at=started_at,
                status="active",
            )
            _ACTIVE_MATCH_BY_SERVER[server.id] = match.id
            logger.info(f"Match 创建: id={match.id}, full_match_id={candidate}")
            return match
        except IntegrityError:
            continue
    raise RuntimeError(f"无法生成唯一 full_match_id: base={base_id}")


async def _apply_state_transition(match: Match, state: str, ts_ms: int, server_id: int) -> Match | None:
    """根据 GameStateChanged 更新 match 或触发关闭。返回更新后的 match（关闭时返回 None）。"""
    # 下一轮 Prematch + 上一局已进过 Playing → 关闭
    if state == _STATE_PREMATCH and match.has_entered_playing:
        await _close_match(match.id, server_id, ts_ms, "prematch_cycle")
        return None

    # 首次进入 Playing：标记 has_entered_playing
    updates: dict = {"current_state": state}
    if state == _STATE_PLAYING and not match.has_entered_playing:
        updates["has_entered_playing"] = True
        match.has_entered_playing = True
    match.current_state = state
    await Match.filter(id=match.id).update(**updates)
    return match


# ---------- Batch 处理 ----------


async def process_batch(batch: IngestBatch) -> IngestResult:
    if _mark_seen(batch.batch_id):
        logger.warning(f"重复批次忽略: batch_id={batch.batch_id}, events={len(batch.events)}")
        return IngestResult(batch_id=batch.batch_id, accepted=0, duplicated=True)

    async with _BATCH_LOCK:
        server = await _resolve_server(batch.server)
        player_infos = _collect_players(batch)
        player_map = await _bulk_upsert_players(player_infos)

        active_match = await _load_active_match(server.id)

        pending: dict[type, list] = {}

        def queue(instance) -> None:
            pending.setdefault(type(instance), []).append(instance)

        # 按 timestamp 排序，确保 Match 状态机按时间推进（ws_service 批次内通常已序，兜底）
        sorted_events = sorted(batch.events, key=lambda e: e.timestamp)

        for event in sorted_events:
            active_match = await _dispatch_event(event, server, player_map, queue, active_match)

        total = 0
        for model_cls, instances in pending.items():
            if not instances:
                continue
            try:
                await model_cls.bulk_create(instances)
                total += len(instances)
            except Exception as exc:
                logger.error(f"bulk_create {model_cls.__name__} 失败: {exc}")
                raise
        logger.info(f"ingest batch {batch.batch_id} 接收 {total}/{len(batch.events)} 条记录")
        return IngestResult(batch_id=batch.batch_id, accepted=total)


def _lookup(player_map: dict[str, Player], info: PlayerInfo | None) -> Player | None:
    if not info or not info.nucleus_hash:
        return None
    return player_map.get(info.nucleus_hash)


async def _dispatch_event(
    event,
    server: Server,
    player_map: dict[str, Player],
    queue,
    active_match: Match | None,
) -> Match | None:
    """分发单个事件；返回处理后的 active_match（可能因关闭/新建而变化）。"""

    if isinstance(event, InitEventIn):
        queue(
            InitEvent(
                timestamp=event.timestamp,
                category=event.category,
                game_version=event.game_version,
                api_version=event.api_version,
                platform=event.platform,
            )
        )
        return active_match

    if isinstance(event, MatchSetupIn):
        # 已有 active match 则先关闭（Prematch 信号若丢失的兜底）
        if active_match:
            await _close_match(active_match.id, server.id, event.timestamp, "new_match")
        new_match = await _create_match(server, event.timestamp, event)
        queue(
            MatchSetup(
                timestamp=event.timestamp,
                category=event.category,
                map_name=event.map_name,
                playlist_name=event.playlist_name,
                playlist_desc=event.playlist_desc,
                datacenter=event.datacenter,
                aim_assist_on=event.aim_assist_on,
                server_id=event.server_id,
                match=new_match,
            )
        )
        return new_match

    if isinstance(event, GameStateChangedIn):
        next_match = active_match
        if active_match:
            next_match = await _apply_state_transition(active_match, event.state, event.timestamp, server.id)
        queue(
            GameStateChanged(
                timestamp=event.timestamp,
                category=event.category,
                state=event.state,
                match=next_match,  # 关闭时写 None，事件仍归属刚结束的一局？—— 故意不绑 old match，保持语义清晰
                server=server,
            )
        )
        return next_match

    if isinstance(event, CharacterSelectedIn):
        player = _lookup(player_map, event.player)
        if player:
            queue(
                CharacterSelected(
                    timestamp=event.timestamp,
                    category=event.category,
                    player=player,
                    player_data=event.player_data,
                    match=active_match,
                    server=server,
                )
            )
        return active_match

    if isinstance(event, PlayerConnectedIn):
        player = _lookup(player_map, event.player)
        if player:
            await _apply_player_connected(player)
            queue(
                PlayerConnected(
                    timestamp=event.timestamp,
                    category=event.category,
                    player=player,
                    player_data=event.player_data,
                    match=active_match,
                    server=server,
                )
            )
        return active_match

    if isinstance(event, PlayerDisconnectedIn):
        player = _lookup(player_map, event.player)
        if player:
            await _apply_player_disconnected(player)
            queue(
                PlayerDisconnected(
                    timestamp=event.timestamp,
                    category=event.category,
                    player=player,
                    player_data=event.player_data,
                    can_reconnect=event.can_reconnect,
                    is_alive=event.is_alive,
                    match=active_match,
                    server=server,
                )
            )
        return active_match

    if isinstance(event, PlayerKilledIn):
        attacker = _lookup(player_map, event.attacker)
        victim = _lookup(player_map, event.victim)
        awarded_to = _lookup(player_map, event.awarded_to)
        queue(
            PlayerKilled(
                timestamp=event.timestamp,
                category=event.category,
                attacker=attacker,
                victim=victim,
                awarded_to=awarded_to,
                attacker_data=event.attacker_data,
                victim_data=event.victim_data,
                awarded_to_data=event.awarded_to_data,
                weapon=event.weapon,
                server=server,
                match=active_match,
            )
        )
        return active_match

    logger.warning(f"未知 ingest 事件类型: {type(event).__name__}")
    return active_match
