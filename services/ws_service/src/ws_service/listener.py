import asyncio
import logging
import uuid
from collections import deque
from typing import Any as TypingAny

import websockets
from google.protobuf import symbol_database
from google.protobuf.any_pb2 import Any
from google.protobuf.json_format import MessageToDict
from shared_lib.config import settings
from shared_lib.schemas.ingest import (
    CharacterSelectedIn,
    GameStateChangedIn,
    IngestBatch,
    InitEventIn,
    MatchSetupIn,
    PlayerConnectedIn,
    PlayerDisconnectedIn,
    PlayerInfo,
    PlayerKilledIn,
    ServerRef,
)
from shared_lib.utils.public_ip import resolve_public_ip
from utils.protos import events_pb2

from .ingest_client import IngestClient

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("LiveAPI")


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
        logger.info(f"批量上报配置: 间隔={self.batch_interval}s, 最大重试={self.batch_max_retries}次, 目标={settings.ws_ingest_base_url}")
        self._flush_task = asyncio.create_task(self._flush_loop())
        self.server = await websockets.serve(self.handle_connection, self.host, self.port)
        try:
            await self.server.wait_closed()
        finally:
            if self._flush_task:
                self._flush_task.cancel()
            # 关闭前刷写剩余数据
            await self._flush()
            await self.ingest.close()

    async def _flush_loop(self):
        """定时批量上报循环"""
        while True:
            await asyncio.sleep(self.batch_interval)
            await self._flush()

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
        logger.info(f"开始上报 {len(events)} 条事件, batch_id={batch.batch_id}")
        ok = await self.ingest.post_batch(batch)
        if ok:
            logger.info(f"上报完成: batch_id={batch.batch_id}, count={len(events)}")
        else:
            logger.error(f"上报失败: batch_id={batch.batch_id}, count={len(events)}, 重新入队等待下一轮")
            # 放回队首，等下次 flush 合并重试；deque maxlen 防止 OOM
            self._pending_events.extendleft(reversed(events))

    def _enqueue(self, event) -> None:
        self._pending_events.append(event)

    async def handle_connection(self, websocket):
        logger.info(f"新连接来自 {websocket.remote_address}")
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
        logger.info(f"[{type_name}] {message}")

    # ==========================================
    # Specific Event Handlers
    # ==========================================

    def on_Init(self, msg):
        logger.info(f"[初始化] 版本: {msg.gameVersion} 平台: {msg.platform}")
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
        logger.info(f"[比赛设置] 地图: {msg.map}, 模式: {msg.playlistName}, 服务器ID: {msg.serverId}")
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
        logger.info(f"[游戏状态变更] 新状态: {msg.state}")
        self._enqueue(GameStateChangedIn(timestamp=msg.timestamp, category=msg.category, state=msg.state))

    def on_MatchStateEnd(self, msg):
        winner_names = [p.name for p in msg.winners]
        logger.info(f"[比赛状态结束] 状态: {msg.state}, 获胜者: {', '.join(winner_names)}")

    def on_RingStartClosing(self, msg):
        logger.info(f"[缩圈开始] 阶段: {msg.stage}, 半径: {msg.currentRadius} -> {msg.endRadius}, 持续时间: {msg.shrinkDuration}秒")

    def on_RingFinishedClosing(self, msg):
        logger.info(f"[缩圈结束] 阶段: {msg.stage}, 半径: {msg.currentRadius}")

    def on_PlayerConnected(self, msg):
        logger.info(f"[玩家连接] {msg.player.name} (队伍 {msg.player.teamId})")
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
        logger.info(f"[玩家断开] {msg.player.name} (可重连: {msg.canReconnect})")
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

    def on_PlayerStatChanged(self, msg):
        ...

    def on_PlayerUpgradeTierChanged(self, msg):
        logger.info(f"[玩家升级] {msg.player.name} 达到等级 {msg.level}")

    def on_PlayerDamaged(self, msg):
        ...

    def on_PlayerKilled(self, msg):
        logger.info(f"[玩家被杀] {msg.attacker.name} 击杀了 {msg.victim.name} 使用武器: {msg.weapon}")
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
                attacker_data=MessageToDict(msg.attacker, preserving_proto_field_name=True) if msg.HasField("attacker") else None,
                victim_data=MessageToDict(msg.victim, preserving_proto_field_name=True) if msg.HasField("victim") else None,
                awarded_to_data=MessageToDict(msg.awardedTo, preserving_proto_field_name=True) if msg.HasField("awardedTo") else None,
                weapon=msg.weapon,
            )
        )

    def on_PlayerDowned(self, msg):
        logger.info(f"[玩家倒地] {msg.attacker.name} 击倒了 {msg.victim.name} 使用武器: {msg.weapon}")

    def on_PlayerAssist(self, msg):
        logger.info(f"[玩家助攻] {msg.assistant.name} 助攻攻击了 {msg.victim.name}")

    def on_SquadEliminated(self, msg):
        logger.info(f"[小队淘汰] 包含 {len(msg.players)} 名玩家的小队被淘汰")

    def on_GibraltarShieldAbsorbed(self, msg):
        logger.info(f"[直布罗陀护盾吸收] {msg.victim.name} 的护盾吸收了来自 {msg.attacker.name} 的 {msg.damageInflicted} 点伤害")

    def on_RevenantForgedShadowDamaged(self, msg):
        logger.info(f"[亡灵暗影吸收] {msg.victim.name} 的暗影吸收了来自 {msg.attacker.name} 的 {msg.damageInflicted} 点伤害")

    def on_PlayerRespawnTeam(self, msg):
        respawned_names = [p.name for p in msg.respawned]
        logger.info(f"[玩家复活队友] {msg.player.name} 复活了: {', '.join(respawned_names)}")

    def on_PlayerRevive(self, msg):
        logger.info(f"[玩家救起] {msg.player.name} 救起了 {msg.revived.name}")

    def on_ArenasItemSelected(self, msg):
        logger.info(f"[竞技场物品选择] {msg.player.name} 选择了 {msg.item} x{msg.quantity}")

    def on_ArenasItemDeselected(self, msg):
        logger.info(f"[竞技场物品取消] {msg.player.name} 取消了 {msg.item} x{msg.quantity}")

    def on_InventoryPickUp(self, msg):
        logger.info(f"[背包拾取] {msg.player.name} 拾取了 {msg.item} x{msg.quantity}")

    def on_InventoryDrop(self, msg):
        logger.info(f"[背包丢弃] {msg.player.name} 丢弃了 {msg.item} x{msg.quantity}")

    def on_InventoryUse(self, msg):
        logger.info(f"[背包使用] {msg.player.name} 使用了 {msg.item} x{msg.quantity}")

    def on_BannerCollected(self, msg):
        logger.info(f"[旗帜收集] {msg.player.name} 收集了 {msg.collected.name} 的旗帜")

    def on_PlayerAbilityUsed(self, msg):
        logger.info(f"[玩家技能使用] {msg.player.name} 使用了 {msg.linkedEntity}")

    def on_LegendUpgradeSelected(self, msg):
        logger.info(f"[传奇升级选择] {msg.player.name} 选择了 {msg.upgradeName} (等级 {msg.level})")

    def on_ZiplineUsed(self, msg):
        logger.info(f"[滑索使用] {msg.player.name} 使用了滑索")

    def on_GrenadeThrown(self, msg):
        logger.info(f"[投掷手雷] {msg.player.name} 投掷了手雷: {msg.linkedEntity}")

    def on_BlackMarketAction(self, msg):
        logger.info(f"[黑市操作] {msg.player.name} 从黑市拿走了 {msg.item}")

    def on_WraithPortal(self, msg):
        logger.info(f"[恶灵传送门] {msg.player.name} 使用了恶灵传送门")

    def on_WarpGateUsed(self, msg):
        logger.info(f"[传送门使用] {msg.player.name} 使用了传送门")

    def on_AmmoUsed(self, msg):
        ...

    def on_WeaponSwitched(self, msg):
        logger.info(f"[武器切换] {msg.player.name} 从 {msg.oldWeapon} 切换到 {msg.newWeapon}")

    def on_CustomEvent(self, msg):
        logger.info(f"[自定义事件] {msg.name} - 数据: {msg.data}")

    def on_ObserverSwitched(self, msg):
        logger.info(f"[观察者切换] 观察者 {msg.observer.name} 切换到了 {msg.target.name}")

    def on_ObserverAnnotation(self, msg):
        logger.info(f"[观察者注释] 序列号: {msg.annotationSerial}")

    def on_CharacterSelected(self, msg):
        logger.info(f"[角色选择] {msg.player.name} 选择了 {msg.player.character}")
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
