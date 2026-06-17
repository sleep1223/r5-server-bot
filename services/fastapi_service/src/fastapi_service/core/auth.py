from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from shared_lib.config import settings
from shared_lib.models import UserBinding

security_scheme = HTTPBearer(auto_error=False)
SUPER_ADMIN_PLATFORM_UID = "1259332131"


async def verify_token(credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme)):
    if not settings.fastapi_access_tokens:
        return
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials.credentials not in settings.fastapi_access_tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials


async def has_valid_token(credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme)) -> bool:
    """公共接口可选鉴权：仅在携带有效 Bearer Token 时返回 True。

    未配置 ``fastapi_access_tokens`` 时一律返回 False（fail-closed），
    避免任意 Bearer 在未配置环境中被当作"管理员"放行。
    """
    if not settings.fastapi_access_tokens:
        return False
    if not credentials:
        return False
    return credentials.credentials in settings.fastapi_access_tokens


async def verify_app_key(x_app_key: str = Header(..., description="用户 AppKey")) -> UserBinding:
    """前端通过 X-App-Key header 认证，返回对应的 UserBinding。"""
    binding = await UserBinding.filter(app_key=x_app_key).prefetch_related("player").first()
    if not binding:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid AppKey")
    return binding


def is_super_admin_binding(binding: UserBinding) -> bool:
    return str(binding.platform_uid or "") == SUPER_ADMIN_PLATFORM_UID


def is_admin_binding(binding: UserBinding) -> bool:
    return is_super_admin_binding(binding) or bool(getattr(binding.player, "is_admin", False))


async def verify_admin_app_key(binding: UserBinding = Depends(verify_app_key)) -> UserBinding:
    if not is_admin_binding(binding):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin permission required")
    return binding


async def verify_super_admin_app_key(binding: UserBinding = Depends(verify_app_key)) -> UserBinding:
    if not is_super_admin_binding(binding):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin permission required")
    return binding
