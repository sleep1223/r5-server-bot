import asyncio

from shared_lib.config import settings

from .listener import LiveAPIListener


async def main() -> None:
    listener = LiveAPIListener(host=settings.ws_host, port=settings.ws_port)
    await listener.start()


if __name__ == "__main__":
    asyncio.run(main())
