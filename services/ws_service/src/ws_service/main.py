import asyncio

from shared_lib.config import settings

from .listener import LiveAPIListener


async def main() -> None:
    listener = LiveAPIListener(
        host=settings.ws_host,
        port=settings.ws_port,
        batch_interval=settings.ws_batch_interval,
        batch_max_retries=settings.ws_batch_max_retries,
        buffer_max=settings.ws_ingest_buffer_max,
    )
    await listener.start()


if __name__ == "__main__":
    asyncio.run(main())
