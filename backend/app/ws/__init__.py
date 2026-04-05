"""WebSocket routes: operator audio ingest and listener fan-out."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

ws_router = APIRouter()


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


@ws_router.websocket("/ingest/{session_id}")
async def operator_ingest_ws(websocket: WebSocket, session_id: str) -> None:
    """Operator chunked-audio upload transport (default ingest path)."""
    await websocket.accept()
    try:
        while True:
            # Placeholder: real impl forwards chunks to TranscriptionProvider.
            await websocket.receive_bytes()
    except WebSocketDisconnect:
        return
