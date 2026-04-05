"""HTTP API routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import sessions

api_router = APIRouter()
api_router.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
