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


async def _upsert_player(info: PlayerInfo | None) -> Player | None:
    if not info or not info.nucleus_hash:
        return None
    defaults: dict = {"name": info.name, "hardware_name": info.hardware_name}
    if info.ip:
        defaults["ip"] = info.ip
    player, _ = await Player.update_or_create(nucleus_hash=info.nucleus_hash, defaults=defaults)
    return player


async def process_batch(batch: IngestBatch) -> IngestResult:
    if _mark_seen(batch.batch_id):
        logger.warning(f"重复批次忽略: batch_id={batch.batch_id}, events={len(batch.events)}")
        return IngestResult(batch_id=batch.batch_id, accepted=0, duplicated=True)

    server = await _resolve_server(batch.server)

    pending: dict[type, list] = {}

    def queue(instance) -> None:
        pending.setdefault(type(instance), []).append(instance)

    for event in batch.events:
        await _dispatch_event(event, server, queue)

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


async def _dispatch_event(event, server: Server, queue) -> None:
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
        player = await _upsert_player(event.player)
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
        player = await _upsert_player(event.player)
        if player:
            player.status = "online"
            await player.save()
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
        player = await _upsert_player(event.player)
        if player:
            if player.status not in ("banned", "kicked"):
                if player.online_at:
                    session_seconds = int((datetime.now(timezone.utc) - player.online_at).total_seconds())
                    if session_seconds > 0:
                        player.total_playtime_seconds += session_seconds
                player.status = "offline"
                await player.save()
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
        attacker = await _upsert_player(event.attacker)
        victim = await _upsert_player(event.victim)
        awarded_to = await _upsert_player(event.awarded_to)
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
