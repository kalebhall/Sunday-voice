"""WebSocket routes: operator audio ingest and listener fan-out."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.ws.operator_audio import operator_audio_router

ws_router = APIRouter()
ws_router.include_router(operator_audio_router)


@ws_router.websocket("/listen/{session_id}/{language}")
async def listener_ws(websocket: WebSocket, session_id: str, language: str) -> None:
    """Anonymous listener subscribes to translated segments for a session+language."""
    await websocket.accept()
    try:
        while True:
            # Placeholder: real impl subscribes to Redis pub/sub fan-out.
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
