"""WebSocket routes: operator audio ingest, operator text ingest, and listener fan-out."""

from __future__ import annotations

from fastapi import APIRouter

from app.ws.listener import listener_router
from app.ws.operator_audio import operator_audio_router
from app.ws.operator_transcript import operator_transcript_router

ws_router = APIRouter()
ws_router.include_router(operator_audio_router)
ws_router.include_router(operator_transcript_router)
ws_router.include_router(listener_router)
