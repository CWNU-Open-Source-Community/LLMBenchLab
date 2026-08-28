"""Composition root for the v1 API."""

from fastapi import APIRouter

from app.api.v1.benchmarks import router as benchmarks_router
from app.api.v1.governance import router as governance_router
from app.api.v1.health import router as health_router
from app.api.v1.leaderboard import router as leaderboard_router
from app.api.v1.models import router as models_router
from app.api.v1.observability import router as observability_router
from app.api.v1.runs import router as runs_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(observability_router)
api_router.include_router(governance_router)
api_router.include_router(models_router)
api_router.include_router(benchmarks_router)
api_router.include_router(runs_router)
api_router.include_router(leaderboard_router)
