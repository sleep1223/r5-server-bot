from fastapi import APIRouter

from . import admin, donations, launcher, leaderboard, player_stats, players, server

router = APIRouter(prefix="/r5", tags=["r5"])

router.include_router(server.router)
router.include_router(players.router)
router.include_router(admin.router)
router.include_router(leaderboard.router)
router.include_router(player_stats.router)
router.include_router(donations.router)
router.include_router(launcher.router)
