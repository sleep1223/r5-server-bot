import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from shared_lib import close_db, init_db
from shared_lib.config import settings

from fastapi_service.api import router as api_router
from fastapi_service.api.v1.r5.api import fetch_server_list_raw_task, sync_players_task


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    sync_task = asyncio.create_task(sync_players_task())
    raw_server_task = asyncio.create_task(fetch_server_list_raw_task())

    yield

    sync_task.cancel()
    raw_server_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass
    
    try:
        await raw_server_task
    except asyncio.CancelledError:
        pass

    await close_db()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.fastapi_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.fastapi_host, port=settings.fastapi_port)
