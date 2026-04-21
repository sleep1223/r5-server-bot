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


IngestEvent = Annotated[
    Union[
        InitEventIn,
        MatchSetupIn,
        GameStateChangedIn,
        CharacterSelectedIn,
        PlayerConnectedIn,
        PlayerDisconnectedIn,
        PlayerKilledIn,
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
