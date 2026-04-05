"""HTTP API routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import auth, sessions

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
