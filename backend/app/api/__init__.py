"""HTTP API routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import auth, sessions, tts, webrtc

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
api_router.include_router(webrtc.router, prefix="/sessions", tags=["webrtc"])
api_router.include_router(tts.router, prefix="/tts", tags=["tts"])
