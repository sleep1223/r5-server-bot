import asyncio
from collections import OrderedDict
from datetime import datetime, timezone

from loguru import logger
from shared_lib.models import (
    CharacterSelected,
    GameStateChanged,
    InitEvent,
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

_SEEN_BATCH_IDS: "OrderedDict[str, None]" = OrderedDict()
_SEEN_BATCH_CAP = 2048

# 批次级锁：ingest 进程内序列化，避免同一玩家在并发批次下出现读改写竞态
# (尤其是 PlayerDisconnected 的 online_at -> total_playtime_seconds 累加)。
# ws_service 30s 一次 flush，竞争极低，串行开销可忽略。
_BATCH_LOCK = asyncio.Lock()


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


async def process_batch(batch: IngestBatch) -> IngestResult:
    if _mark_seen(batch.batch_id):
        logger.warning(f"重复批次忽略: batch_id={batch.batch_id}, events={len(batch.events)}")
        return IngestResult(batch_id=batch.batch_id, accepted=0, duplicated=True)

    async with _BATCH_LOCK:
        server = await _resolve_server(batch.server)
        player_infos = _collect_players(batch)
        player_map = await _bulk_upsert_players(player_infos)

        pending: dict[type, list] = {}

        def queue(instance) -> None:
            pending.setdefault(type(instance), []).append(instance)

        for event in batch.events:
            await _dispatch_event(event, server, player_map, queue)

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


async def _dispatch_event(event, server: Server, player_map: dict[str, Player], queue) -> None:
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
        return

    if isinstance(event, MatchSetupIn):
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
            )
        )
        return

    if isinstance(event, GameStateChangedIn):
        queue(GameStateChanged(timestamp=event.timestamp, category=event.category, state=event.state))
        return

    if isinstance(event, CharacterSelectedIn):
        player = _lookup(player_map, event.player)
        if player:
            queue(
                CharacterSelected(
                    timestamp=event.timestamp,
                    category=event.category,
                    player=player,
                    player_data=event.player_data,
                )
            )
        return

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
                )
            )
        return

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
                )
            )
        return

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
            )
        )
        return

    logger.warning(f"未知 ingest 事件类型: {type(event).__name__}")
