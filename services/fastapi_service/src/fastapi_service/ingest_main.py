"""独立运行的 ingest FastAPI app。

- 只挂载 ``/v1/r5/ingest/*`` 路由
- ``workers=1``：保证 `_SEEN_BATCH_IDS` 去重 LRU 和 `_BATCH_LOCK` 有全局唯一性
- 不启动 task_scheduler，不挂载查询路由；schema 生成交给主 app
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from shared_lib import close_db, init_db

from fastapi_service.api.v1.ingest import router as ingest_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # schema 由主 app 生成，这里只连 DB，避免两个进程并发 DDL。
    await init_db(generate_schemas=False)
    yield
    await close_db()


app = FastAPI(lifespan=lifespan, title="r5-ingest")
app.include_router(ingest_router, prefix="/v1/r5")
