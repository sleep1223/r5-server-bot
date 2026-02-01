import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from shared_lib import close_db, init_db

from fastapi_service.api import router as api_router
from fastapi_service.api.v1.r5.api import sync_players_task


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    sync_task = asyncio.create_task(sync_players_task())

    yield

    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass

    await close_db()


app = FastAPI(lifespan=lifespan)
app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn
    from shared_lib.config import settings

    uvicorn.run(app, host=settings.fastapi_host, port=settings.fastapi_port)
