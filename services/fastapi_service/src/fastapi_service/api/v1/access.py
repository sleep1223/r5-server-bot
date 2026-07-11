from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from shared_lib.config import settings

from fastapi_service.core.auth import security_scheme
from fastapi_service.services import player_access_service

router = APIRouter(prefix="/access", tags=["r5-access"])


class PlayerAccessRequest(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    uid: str
    nucleusId: int
    playerName: str
    ip: str
    port: int
    serverName: str | None = None
    serverIp: str | None = None
    serverPort: int | None = None


class PlayerAccessResponse(BaseModel):
    allow: bool
    reason: str | None = None
    ruleId: str | None = None
    action: Literal["kick", "ban"] | None = None
    reasonCode: str | None = None
    ip: str | None = None
    processedAt: str | None = None
    canSelfUnban: bool = False


class OnlinePlayer(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    uid: str
    nucleusId: int
    playerName: str
    ip: str | None = None
    port: int | None = None
    userId: int | None = None
    handle: int | None = None
    signonState: int | None = None
    country: str | None = None
    region: str | None = None
    inputDevice: str | None = None
    input_device: str | None = None
    input: str | None = None
    device: str | None = None


class OnlinePlayersRequest(BaseModel):
    serverName: str | None = None
    serverIp: str | None = None
    serverPort: int | None = None
    map: str | None = None
    tick: int | None = None
    numPlayers: int | None = None
    maxPlayers: int | None = None
    players: list[OnlinePlayer] = Field(default_factory=list)


class OnlinePlayerAction(BaseModel):
    uid: str
    action: Literal["kick", "ban"]
    nucleusId: int | None = None
    reason: str | None = None
    reasonCode: str | None = None
    ip: str | None = None
    processedAt: str | None = None
    ruleId: str | None = None


class OnlinePlayersResponse(BaseModel):
    actions: list[OnlinePlayerAction] = []


def _verify_optional_access_token(credentials: HTTPAuthorizationCredentials | None) -> None:
    if not credentials:
        return
    if settings.fastapi_access_tokens and credentials.credentials not in settings.fastapi_access_tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证令牌无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.post("/check", response_model=PlayerAccessResponse)
async def check_player_access(
    payload: PlayerAccessRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> PlayerAccessResponse:
    _verify_optional_access_token(credentials)
    decision = await player_access_service.check_player_access(
        uid=payload.uid,
        nucleus_id=payload.nucleusId,
        player_name=payload.playerName,
        ip=payload.ip,
        port=payload.port,
        server_id=None,
        server_ip=payload.serverIp,
        server_port=payload.serverPort,
        server_name=payload.serverName,
    )
    return PlayerAccessResponse(
        allow=bool(decision["allow"]),
        reason=decision.get("reason"),
        ruleId=decision.get("rule_id"),
        action=player_access_service.action_from_access_decision(decision),
        reasonCode=decision.get("reason_code"),
        ip=decision.get("ip"),
        processedAt=decision.get("processed_at"),
        canSelfUnban=decision.get("source") == "kick_notice",
    )


@router.post("/online", response_model=OnlinePlayersResponse)
async def report_online_players(
    payload: OnlinePlayersRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> OnlinePlayersResponse:
    _verify_optional_access_token(credentials)
    result = await player_access_service.process_online_players_report(
        server_id="",
        report=payload.model_dump(),
    )
    return OnlinePlayersResponse(
        actions=[OnlinePlayerAction(**action) for action in result["actions"]],
    )
