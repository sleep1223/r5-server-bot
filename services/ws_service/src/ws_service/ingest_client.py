import asyncio
import logging

import httpx
from shared_lib.schemas.ingest import IngestBatch

logger = logging.getLogger("LiveAPI.ingest")


class IngestClient:
    def __init__(self, base_url: str, token: str, timeout: float, max_retries: int):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def post_batch(self, batch: IngestBatch) -> bool:
        """POST 一个 batch，失败按 exp backoff 重试，全部失败返回 False。"""
        url = f"{self.base_url}/events"
        payload = batch.model_dump(mode="json")
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self._client.post(url, json=payload)
                resp.raise_for_status()
                return True
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                logger.error(f"ingest POST {url} 状态码={status} (第 {attempt}/{self.max_retries} 次): {exc.response.text[:500]}")
                if 400 <= status < 500 and status not in (408, 429):
                    return False  # 客户端错误不重试，直接丢弃
            except Exception as exc:
                logger.error(f"ingest POST {url} 失败 (第 {attempt}/{self.max_retries} 次): {exc}")
            if attempt < self.max_retries:
                await asyncio.sleep(min(2**attempt, 10))
        return False
