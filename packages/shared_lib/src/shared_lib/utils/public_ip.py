import httpx
from loguru import logger

_FALLBACK_URLS = (
    "https://ipv4.icanhazip.com",
    "https://ipinfo.io/ip",
    "https://ipinfo.io/ip",
    "https://ifconfig.me/ip",
)


async def resolve_public_ip(override: str = "", timeout: float = 3.0) -> str:
    """返回本机公网 IP。

    优先使用 override（env 配置），否则按顺序请求 _FALLBACK_URLS。
    全部失败时抛 RuntimeError，以便调用方可以 fail-fast。
    """
    if override:
        return override.strip()

    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for url in _FALLBACK_URLS:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                ip = resp.text.strip()
                if ip:
                    logger.info(f"公网 IP 解析成功: 来源={url}, ip={ip}")
                    return ip
            except Exception as e:
                last_error = e
                logger.warning(f"公网 IP 探测失败: 来源={url}, error={e}")

    raise RuntimeError(f"无法从任何公共 IP 服务解析公网 IP: {last_error}")
