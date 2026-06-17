from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from shared_lib.config import settings

from fastapi_service.core.auth import security_scheme, verify_token
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, success
from fastapi_service.core.utils import check_is_admin
from fastapi_service.services import server_service

router = APIRouter()


class ServerAliasBody(BaseModel):
    short_name: str | None = None


@router.get("/server")
async def get_server_list(
    server_name: str | None = None,
    simple: bool = False,
    cn_only: bool = False,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
):
    """合并后的服务器列表查询接口。

    参数：
    - ``server_name``: 按服务器名模糊过滤（不区分大小写）。
    - ``simple``: 精简字段，省略在线玩家列表等重字段。
    - ``cn_only``: 只返回已通过 RCON 同步到本地的服务器（通常是中国服）。
    """
    is_admin = check_is_admin(credentials, settings.fastapi_access_tokens)
    results = await server_service.list_servers(
        server_name=server_name,
        simple=simple,
        cn_only=cn_only,
        is_admin=is_admin,
    )
    return success(data=results, msg=f"{len(results)} servers")


@router.get("/server/info", dependencies=[Depends(verify_token)])
async def get_server_info():
    results = server_service.get_server_info()
    return success(data=results, msg="Server info retrieved")


@router.patch("/server/by-host/{host}/alias", dependencies=[Depends(verify_token)])
async def set_server_alias(host: str, body: ServerAliasBody):
    """设置或清空指定 host 的短名/别名。空字符串或 null 视为清空。"""
    result, err = await server_service.set_server_alias(host, body.short_name)
    if err == "not_found":
        return error(ErrorCode.SERVER_NOT_FOUND, f"Server not found: {host}")
    if err == "alias_conflict":
        conflict_host = result["host"] if result else ""
        return error(ErrorCode.SERVER_NOT_FOUND, f"Alias already used by host {conflict_host}")

    return success(data=result, msg="alias updated")
