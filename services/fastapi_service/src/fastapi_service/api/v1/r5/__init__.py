from fastapi import APIRouter

from .api import router as api_router
from .donation import router as donation_router

router = APIRouter()

router.include_router(api_router, prefix="/r5", tags=["r5"])
router.include_router(donation_router, prefix="/r5", tags=["r5"])
