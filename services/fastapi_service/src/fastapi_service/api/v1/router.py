from fastapi import APIRouter

from . import access, admin, admin_management, apex, donations, launcher, leaderboard, matches, player_stats, players, server, teams, user

router = APIRouter(prefix="/r5", tags=["r5"])

router.include_router(server.router)
router.include_router(players.router)
router.include_router(access.router)
router.include_router(admin.router)
router.include_router(admin_management.router)
router.include_router(admin_management.bot_router)
router.include_router(apex.router)
router.include_router(leaderboard.router)
router.include_router(matches.router)
router.include_router(player_stats.router)
router.include_router(donations.router)
router.include_router(launcher.router)
router.include_router(user.router)
router.include_router(teams.router)
