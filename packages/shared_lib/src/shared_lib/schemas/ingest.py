from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class PlayerInfo(BaseModel):
    nucleus_hash: str
    name: str = ""
    hardware_name: str | None = None
    ip: str | None = None


class ServerRef(BaseModel):
    host: str
    port: int = 37015
    name: str | None = None


class _BaseEventIn(BaseModel):
    timestamp: int
    category: str


class InitEventIn(_BaseEventIn):
    type: Literal["init"] = "init"
    game_version: str
    api_version: dict
    platform: str


class MatchSetupIn(_BaseEventIn):
    type: Literal["match_setup"] = "match_setup"
    map_name: str
    playlist_name: str
    playlist_desc: str
    datacenter: dict
    aim_assist_on: bool
    server_id: str


class GameStateChangedIn(_BaseEventIn):
    type: Literal["game_state_changed"] = "game_state_changed"
    state: str


class MatchStateEndIn(_BaseEventIn):
    type: Literal["match_state_end"] = "match_state_end"
    state: str  # 通常是 "WinnerDetermined"
    winners: list[dict] | None = None


class CharacterSelectedIn(_BaseEventIn):
    type: Literal["character_selected"] = "character_selected"
    player: PlayerInfo
    player_data: dict


class PlayerConnectedIn(_BaseEventIn):
    type: Literal["player_connected"] = "player_connected"
    player: PlayerInfo
    player_data: dict


class PlayerDisconnectedIn(_BaseEventIn):
    type: Literal["player_disconnected"] = "player_disconnected"
    player: PlayerInfo
    player_data: dict
    can_reconnect: bool | None = None
    is_alive: bool | None = None


class PlayerKilledIn(_BaseEventIn):
    type: Literal["player_killed"] = "player_killed"
    attacker: PlayerInfo | None = None
    victim: PlayerInfo | None = None
    awarded_to: PlayerInfo | None = None
    attacker_data: dict | None = None
    victim_data: dict | None = None
    awarded_to_data: dict | None = None
    weapon: str


class ConnectionBoundaryIn(_BaseEventIn):
    """ws_service 自生成的信号：游戏服的 LiveAPI WebSocket 连接生命周期边界。

    r5 游戏服的 DirtySDK WS 客户端每 ~50s 自动断开重连一次；在 `fs_1v1` / 自定义房
    playlist 下，这个周期大致对应一"波"并行 duel 的生命周期，可当作 match 边界。
    由 ws_service 在 `handle_connection` 的 finally 发出，立即 flush 不等 batch。
    """

    type: Literal["connection_boundary"] = "connection_boundary"
    reason: str  # "ws_closed"（目前只需要 closed；opened 由下次 Playing/PlayerKilled 触发 synth）


IngestEvent = Annotated[
    Union[
        InitEventIn,
        MatchSetupIn,
        GameStateChangedIn,
        MatchStateEndIn,
        CharacterSelectedIn,
        PlayerConnectedIn,
        PlayerDisconnectedIn,
        PlayerKilledIn,
        ConnectionBoundaryIn,
    ],
    Field(discriminator="type"),
]


class IngestBatch(BaseModel):
    batch_id: str
    server: ServerRef
    events: list[IngestEvent]


class IngestResult(BaseModel):
    batch_id: str
    accepted: int
    duplicated: bool = False
