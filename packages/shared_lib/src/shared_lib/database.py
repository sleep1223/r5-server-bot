from tortoise import Tortoise

from shared_lib.config import settings

TORTOISE_ORM = settings.tortoise_orm


async def init_db(config: dict | None = None) -> None:
    await Tortoise.init(config=config or TORTOISE_ORM)
    # Note: In production, use migrations (aerich). generate_schemas is for dev/testing.
    await Tortoise.generate_schemas()


async def close_db() -> None:
    await Tortoise.close_connections()
