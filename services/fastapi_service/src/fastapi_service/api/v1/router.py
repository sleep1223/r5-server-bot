from fastapi import APIRouter

from . import access, admin, admin_management, donations, launcher, leaderboard, matches, player_stats, players, server, teams, user

router = APIRouter(prefix="/r5", tags=["r5"])

router.include_router(server.router)
router.include_router(players.router)
router.include_router(access.router)
router.include_router(admin.router)
router.include_router(admin_management.router)
router.include_router(leaderboard.router)
router.include_router(matches.router)
router.include_router(player_stats.router)
router.include_router(donations.router)
router.include_router(launcher.router)
router.include_router(user.router)
router.include_router(teams.router)
# ingest 路由拆到独立 app (fastapi_service.ingest_main)，
# 由独立 Granian 进程跑，避免进程内去重 LRU 跨 worker 失效。
