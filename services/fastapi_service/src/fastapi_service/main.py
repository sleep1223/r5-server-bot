import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from shared_lib import close_db, init_db
from shared_lib.config import settings

from fastapi_service.api import router as api_router
from fastapi_service.tasks.scheduler import task_scheduler


def _configure_logging() -> None:
    log_level = (settings.log_level or "INFO").upper()
    logger.remove()
    logger.add(sys.stderr, level=log_level)


_configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await task_scheduler.start()

    yield

    await task_scheduler.stop()
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
