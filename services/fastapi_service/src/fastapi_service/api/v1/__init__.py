from fastapi import APIRouter

from .r5 import router as r5_router

router = APIRouter()

router.include_router(r5_router)
