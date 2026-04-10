from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials
from shared_lib.config import settings

from fastapi_service.core.auth import security_scheme, verify_token
from fastapi_service.core.response import success
from fastapi_service.core.utils import check_is_admin
from fastapi_service.services import server_service

router = APIRouter()


@router.get("/server")
async def get_raw_server_list():
    """获取原始服务器列表缓存数据，无需鉴权"""
    return success(data=server_service.get_raw_server_list(), msg="Raw server list retrieved")


@router.get("/server/info", dependencies=[Depends(verify_token)])
async def get_server_info():
    results = server_service.get_server_info()
    return success(data=results, msg="Server info retrieved")


@router.get("/server/status")
async def get_server_status(
    server_name: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
):
    """获取所有已连接服务器或特定服务器的状态。"""
    is_admin = check_is_admin(credentials, settings.fastapi_access_tokens)
    results = server_service.get_server_status(server_name=server_name, is_admin=is_admin)
    return success(data=results, msg=f"Server status for {len(results)} servers")
