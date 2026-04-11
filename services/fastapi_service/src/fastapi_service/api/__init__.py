from fastapi import APIRouter

from .v1.pylon import router as pylon_router
from .v1.router import router as v1_router

router = APIRouter()
router.include_router(v1_router, prefix="/v1", tags=["v1"])
# Pylon master-server endpoints live directly under /v1 (no /r5 prefix) so the
# canonical paths match what the R5 SDK clients/servers expect, e.g.
# https://r5.sleep0.de/api/v1/client/auth and /v1/server/auth/keyinfo.
router.include_router(pylon_router, prefix="/v1")
