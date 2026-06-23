from fastapi import APIRouter

from .v1 import access, matches
from .v1.router import router as v1_router

router = APIRouter()
router.include_router(v1_router, prefix="/v1", tags=["v1"])
router.include_router(v1_router, prefix="/api", tags=["api"])
router.include_router(access.router, prefix="/r5", tags=["r5-access"])
router.include_router(matches.router, prefix="/r5", tags=["r5-matches"])
