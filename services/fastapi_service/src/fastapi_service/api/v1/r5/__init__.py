from fastapi import APIRouter

from .api import router as api_router

router = APIRouter()

router.include_router(api_router, prefix="/r5", tags=["r5"])
