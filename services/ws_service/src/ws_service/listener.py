import asyncio
import uuid
from collections import deque
from typing import Any as TypingAny

import websockets
from google.protobuf import symbol_database
from google.protobuf.any_pb2 import Any
from google.protobuf.json_format import MessageToDict
from loguru import logger
from shared_lib.config import settings
from shared_lib.schemas.ingest import (
    CharacterSelectedIn,
    GameStateChangedIn,
    IngestBatch,
    InitEventIn,
    MatchSetupIn,
    MatchStateEndIn,
    PlayerConnectedIn,
    PlayerDisconnectedIn,
    PlayerInfo,
    PlayerKilledIn,
    ServerRef,
)
from shared_lib.utils.public_ip import resolve_public_ip
from utils.protos import events_pb2

from .display import BURST_FLUSH_INTERVAL, HEARTBEAT_INTERVAL, aggregator, match_tracker
from .ingest_client import IngestClient


def _player_info(msg) -> PlayerInfo | None:
    if not msg or not getattr(msg, "nucleusHash", ""):
        return None
    return PlayerInfo(
        nucleus_hash=msg.nucleusHash,
        name=getattr(msg, "name", "") or "",
        hardware_name=getattr(msg, "hardwareName", None) or None,
        ip=getattr(msg, "ip", None) or None,
    )


class LiveAPIListener:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7771,
        batch_interval: int = 30,
        batch_max_retries: int = 3,
        ingest_client: IngestClient | None = None,
        server_ref: ServerRef | None = None,
        buffer_max: int = 100_000,
    ):
        self.host = host
        self.port = port
        self.server = None
        self.batch_interval = batch_interval
        self.batch_max_retries = batch_max_retries
        self.ingest = ingest_client
        self.server_ref = server_ref
        self._pending_events: deque = deque(maxlen=buffer_max)
        self._flush_task: asyncio.Task | None = None
        self._burst_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

    async def start(self):
        if self.ingest is None:
            self.ingest = IngestClient(
                base_url=settings.ws_ingest_base_url,
                token=settings.ws_ingest_token,
                timeout=settings.ws_ingest_timeout,
                max_retries=self.batch_max_retries,
            )
        if self.server_ref is None:
            public_ip = await resolve_public_ip(settings.ws_public_ip)
            self.server_ref = ServerRef(
                host=public_ip,
                port=settings.ws_public_port,
                name=f"server-{public_ip}",
            )
            logger.info(f"本机 Server 上报标识: host={public_ip}, port={settings.ws_public_port}")

        logger.info(f"正在启动 LiveAPI 监听器于 {self.host}:{self.port}")
        logger.info(
            f"批量上报配置: 间隔={self.batch_interval}s, 最大重试={self.batch_max_retries}次, "
            f"目标={settings.ws_ingest_base_url}"
        )
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._burst_task = asyncio.create_task(self._burst_flush_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self.server = await websockets.serve(self.handle_connection, self.host, self.port)
        try:
            await self.server.wait_closed()
        finally:
            for task in (self._flush_task, self._burst_task, self._heartbeat_task):
                if task:
                    task.cancel()
            # 关闭前刷写剩余数据
            await self._flush()
            aggregator.flush()
            await self.ingest.close()

    async def _flush_loop(self):
        """定时批量上报循环"""
        while True:
            await asyncio.sleep(self.batch_interval)
            await self._flush()

    async def _burst_flush_loop(self):
        """爆发事件聚合输出循环"""
        while True:
            await asyncio.sleep(BURST_FLUSH_INTERVAL)
            try:
                aggregator.flush()
            except Exception as e:
                logger.error(f"aggregator.flush 失败: {e}")

    async def _heartbeat_loop(self):
        """当前比赛心跳循环"""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                match_tracker.heartbeat()
            except Exception as e:
                logger.error(f"match_tracker.heartbeat 失败: {e}")

    async def _flush(self):
        """将缓存的事件批量 POST 给 FastAPI ingest 接口。"""
        if not self._pending_events:
            return
        if self.ingest is None or self.server_ref is None:
            logger.error("ingest client 未初始化，丢弃当前批次")
            self._pending_events.clear()
            return

        events = list(self._pending_events)
        self._pending_events.clear()

        batch = IngestBatch(
            batch_id=str(uuid.uuid4()),
            server=self.server_ref,
            events=events,  # type: ignore[arg-type]
        )
        logger.debug(f"开始上报 {len(events)} 条事件, batch_id={batch.batch_id}")
        ok = await self.ingest.post_batch(batch)
        if ok:
            logger.info(f"📤 上报完成 batch_id={batch.batch_id[:8]} count={len(events)}")
        else:
            logger.error(
                f"上报失败: batch_id={batch.batch_id}, count={len(events)}, 重新入队等待下一轮"
            )
            # 放回队首，等下次 flush 合并重试；deque maxlen 防止 OOM
            self._pending_events.extendleft(reversed(events))

    def _enqueue(self, event) -> None:
        self._pending_events.append(event)

    async def handle_connection(self, websocket):
        logger.info(f"🔗 新连接 {websocket.remote_address}")
        try:
            async for message in websocket:
                self.parse_message(message)
        except websockets.exceptions.ConnectionClosed:
            logger.info("连接已关闭")
        except Exception as e:
            logger.error(f"连接处理程序错误: {e}")

    def parse_message(self, data):
        try:
            # 如果接收到的是字符串（websockets 在文本帧时返回 str），尝试将其编码回 bytes
            # 因为 Protobuf 解析需要 bytes 类型
            if isinstance(data, str):
                data = data.encode("utf-8")

            # 从 events_pb2 模块中动态获取 LiveAPIEvent 类
            event_class = getattr(events_pb2, "LiveAPIEvent", None)
            if event_class is None:
                logger.error("在 events_pb2 中找不到 LiveAPIEvent 类")
                return
            event = event_class()
            event.ParseFromString(data)

            if event.HasField("gameMessage"):
                self.process_game_message(event.gameMessage)
            else:
                logger.warning("收到没有 gameMessage 的 LiveAPIEvent")

        except Exception as e:
            logger.error(f"解析消息失败: {e}")

    def process_game_message(self, any_msg: Any):
        try:
            # Get the type name from the type URL
            type_name = any_msg.TypeName()  # e.g. "rtech.liveapi.PlayerKilled"

            # Use symbol_database to find the message class
            sym_db = symbol_database.Default()

            try:
                msg_class = sym_db.GetSymbol(type_name)
            except KeyError:
                logger.warning(f"未知消息类型: {type_name}")
                return

            msg_instance = msg_class()
            if any_msg.Unpack(msg_instance):
                # Dispatch to specific handler if available
                short_name = type_name.split(".")[-1]
                handler_name = f"on_{short_name}"

                if hasattr(self, handler_name):
                    getattr(self, handler_name)(msg_instance)
                else:
                    self.on_event(type_name, msg_instance)
            else:
                logger.error(f"解包失败 {type_name}")

        except Exception as e:
            logger.error(f"处理游戏消息错误: {e}")

    def on_event(self, type_name: str, message: TypingAny) -> None:
        """Default handler for unhandled events"""
        logger.debug(f"[{type_name}] {message}")

    # ==========================================
    # 里程碑事件（独立打印 + 入队上报）
    # ==========================================

    def on_Init(self, msg):
        logger.info(f"🛠  INIT 版本={msg.gameVersion} 平台={msg.platform}")
        self._enqueue(
            InitEventIn(
                timestamp=msg.timestamp,
                category=msg.category,
                game_version=msg.gameVersion,
                api_version=MessageToDict(msg.apiVersion, preserving_proto_field_name=True),
                platform=msg.platform,
            )
        )

    def on_MatchSetup(self, msg):
        match_tracker.on_match_setup(
            server_id=msg.serverId,
            map_name=msg.map,
            playlist_name=msg.playlistName,
            playlist_desc=msg.playlistDesc,
            started_ts=msg.timestamp,
        )
        self._enqueue(
            MatchSetupIn(
                timestamp=msg.timestamp,
                category=msg.category,
                map_name=msg.map,
                playlist_name=msg.playlistName,
                playlist_desc=msg.playlistDesc,
                datacenter=MessageToDict(msg.datacenter, preserving_proto_field_name=True),
                aim_assist_on=msg.aimAssistOn,
                server_id=msg.serverId,
            )
        )

    def on_GameStateChanged(self, msg):
        logger.info(f"▶️  STATE → {msg.state}")
        self._enqueue(
            GameStateChangedIn(timestamp=msg.timestamp, category=msg.category, state=msg.state)
        )

    def on_MatchStateEnd(self, msg):
        winner_names = [p.name for p in msg.winners]
        match_tracker.on_match_end(winner_names)
        winners = [MessageToDict(p, preserving_proto_field_name=True) for p in msg.winners]
        self._enqueue(
            MatchStateEndIn(
                timestamp=msg.timestamp,
                category=msg.category,
                state=msg.state,
                winners=winners,
            )
        )

    def on_RingStartClosing(self, msg):
        logger.info(
            f"⭕ ring 开始 stage={msg.stage} "
            f"radius={msg.currentRadius}→{msg.endRadius} duration={msg.shrinkDuration}s"
        )

    def on_RingFinishedClosing(self, msg):
        logger.info(f"⭕ ring 收缩完成 stage={msg.stage} radius={msg.currentRadius}")

    def on_PlayerConnected(self, msg):
        name = msg.player.name
        logger.info(f"🔌+ {name} (teamId={msg.player.teamId})")
        match_tracker.on_player_connected(name)
        info = _player_info(msg.player)
        if not info:
            return
        self._enqueue(
            PlayerConnectedIn(
                timestamp=msg.timestamp,
                category=msg.category,
                player=info,
                player_data=MessageToDict(msg.player, preserving_proto_field_name=True),
            )
        )

    def on_PlayerDisconnected(self, msg):
        name = msg.player.name
        logger.info(f"🔌- {name} (reconnect={msg.canReconnect})")
        match_tracker.on_player_disconnected(name)
        info = _player_info(msg.player)
        if not info:
            return
        self._enqueue(
            PlayerDisconnectedIn(
                timestamp=msg.timestamp,
                category=msg.category,
                player=info,
                player_data=MessageToDict(msg.player, preserving_proto_field_name=True),
                can_reconnect=msg.canReconnect,
                is_alive=getattr(msg, "isAlive", None),
            )
        )

    def on_CharacterSelected(self, msg):
        logger.info(f"🎭 {msg.player.name} 选择了 {msg.player.character}")
        info = _player_info(msg.player)
        if not info:
            return
        self._enqueue(
            CharacterSelectedIn(
                timestamp=msg.timestamp,
                category=msg.category,
                player=info,
                player_data=MessageToDict(msg.player, preserving_proto_field_name=True),
            )
        )

    # ==========================================
    # 爆发类事件（聚合输出 + 入队上报）
    # ==========================================

    def on_PlayerKilled(self, msg):
        aggregator.add("kill", actor=msg.attacker.name if msg.HasField("attacker") else None)
        attacker = _player_info(msg.attacker) if msg.HasField("attacker") else None
        victim = _player_info(msg.victim) if msg.HasField("victim") else None
        awarded_to = _player_info(msg.awardedTo) if msg.HasField("awardedTo") else None
        self._enqueue(
            PlayerKilledIn(
                timestamp=msg.timestamp,
                category=msg.category,
                attacker=attacker,
                victim=victim,
                awarded_to=awarded_to,
                attacker_data=MessageToDict(msg.attacker, preserving_proto_field_name=True)
                if msg.HasField("attacker")
                else None,
                victim_data=MessageToDict(msg.victim, preserving_proto_field_name=True)
                if msg.HasField("victim")
                else None,
                awarded_to_data=MessageToDict(msg.awardedTo, preserving_proto_field_name=True)
                if msg.HasField("awardedTo")
                else None,
                weapon=msg.weapon,
            )
        )

    def on_PlayerDowned(self, msg):
        aggregator.add("downed", actor=getattr(msg.attacker, "name", None) or None)

    def on_PlayerAssist(self, msg):
        aggregator.add("assist", actor=getattr(msg.assistant, "name", None) or None)

    def on_PlayerRevive(self, msg):
        aggregator.add("revive", actor=getattr(msg.player, "name", None) or None)

    def on_PlayerDamaged(self, msg):
        aggregator.add("damaged")

    def on_InventoryPickUp(self, msg):
        aggregator.add("pickup")

    def on_InventoryDrop(self, msg):
        aggregator.add("drop")

    def on_InventoryUse(self, msg):
        aggregator.add("use")

    def on_GrenadeThrown(self, msg):
        aggregator.add("grenade", actor=getattr(msg.player, "name", None) or None)

    def on_ZiplineUsed(self, msg):
        aggregator.add("zipline", actor=getattr(msg.player, "name", None) or None)

    def on_AmmoUsed(self, msg):  # 高频无用事件，静默
        ...

    def on_PlayerStatChanged(self, msg):  # 静默
        ...

    # ==========================================
    # 其他不频繁的事件保持普通日志
    # ==========================================

    def on_PlayerUpgradeTierChanged(self, msg):
        logger.info(f"⬆️  {msg.player.name} 升级 Lv.{msg.level}")

    def on_SquadEliminated(self, msg):
        logger.info(f"❎ 小队淘汰 ({len(msg.players)} 人)")

    def on_GibraltarShieldAbsorbed(self, msg):
        logger.debug(
            f"🛡  {msg.victim.name} 护盾吸收了 {msg.attacker.name} 的 {msg.damageInflicted} dmg"
        )

    def on_RevenantForgedShadowDamaged(self, msg):
        logger.debug(
            f"👻 {msg.victim.name} 暗影吸收 {msg.attacker.name} 的 {msg.damageInflicted} dmg"
        )

    def on_PlayerRespawnTeam(self, msg):
        respawned_names = [p.name for p in msg.respawned]
        logger.info(f"🔁 {msg.player.name} 复活: {', '.join(respawned_names)}")

    def on_ArenasItemSelected(self, msg):
        logger.debug(f"🎯 {msg.player.name} 选 {msg.item}×{msg.quantity}")

    def on_ArenasItemDeselected(self, msg):
        logger.debug(f"🎯 {msg.player.name} 取消 {msg.item}×{msg.quantity}")

    def on_BannerCollected(self, msg):
        logger.info(f"🏳  {msg.player.name} 收集了 {msg.collected.name} 的旗帜")

    def on_PlayerAbilityUsed(self, msg):
        logger.debug(f"✨ {msg.player.name} 使用技能 {msg.linkedEntity}")

    def on_LegendUpgradeSelected(self, msg):
        logger.info(f"⬆️  {msg.player.name} 升级 {msg.upgradeName} (Lv.{msg.level})")

    def on_BlackMarketAction(self, msg):
        logger.info(f"🏪 {msg.player.name} 黑市买入 {msg.item}")

    def on_WraithPortal(self, msg):
        logger.info(f"🌀 {msg.player.name} 使用了恶灵传送门")

    def on_WarpGateUsed(self, msg):
        logger.info(f"🌀 {msg.player.name} 使用了传送门")

    def on_WeaponSwitched(self, msg):
        logger.debug(f"🔫 {msg.player.name} {msg.oldWeapon} → {msg.newWeapon}")

    def on_CustomEvent(self, msg):
        logger.info(f"📌 {msg.name} - {msg.data}")

    def on_ObserverSwitched(self, msg):
        logger.debug(f"👁  观察者 {msg.observer.name} → {msg.target.name}")

    def on_ObserverAnnotation(self, msg):
        logger.debug(f"👁  annotation serial={msg.annotationSerial}")
