from tortoise import Tortoise

from shared_lib.config import settings

TORTOISE_ORM = settings.tortoise_orm


async def init_db(config: dict | None = None, *, generate_schemas: bool = True) -> None:
    await Tortoise.init(config=config or TORTOISE_ORM)
    # Note: In production, use migrations (aerich). generate_schemas is for dev/testing.
    # 多进程部署时，只让主进程跑 generate_schemas，其余进程传 generate_schemas=False
    # 避免 DDL 竞态。
    if generate_schemas:
        await Tortoise.generate_schemas()


async def close_db() -> None:
    await Tortoise.close_connections()
