from fastapi import APIRouter

from .endpoints.admin import router as admin_router
from .endpoints.launcher import router as launcher_router
from .endpoints.leaderboard import router as leaderboard_router
from .endpoints.players import router as players_router
from .endpoints.server import router as server_router

router = APIRouter()
router.include_router(server_router)
router.include_router(players_router)
router.include_router(admin_router)
router.include_router(leaderboard_router)
router.include_router(launcher_router)
