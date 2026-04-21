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
                    logger.info(f"Resolved public IP via {url}: {ip}")
                    return ip
            except Exception as e:
                last_error = e
                logger.warning(f"Public IP probe failed via {url}: {e}")

    raise RuntimeError(f"Failed to resolve public IP from any provider: {last_error}")
