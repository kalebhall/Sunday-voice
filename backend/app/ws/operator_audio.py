"""WebSocket endpoint: operator audio ingest.

``/ws/operator/{session_id}/audio?token=<jwt>``

Receives ~2-3 s WebM/Opus chunks from the browser's MediaRecorder, forwards
them to the configured :class:`TranscriptionProvider`, and publishes
:class:`TranscriptEvent` objects onto the in-process asyncio pub/sub.

Invariants enforced:
* **Operator JWT** — validated from query param before the socket is accepted.
* **Single-operator-per-session lock** — only one concurrent ingest connection
  per session; a second attempt is rejected with code 4409 (conflict).
* **Backpressure** — an :class:`asyncio.Queue` with a bounded depth sits
  between the receive loop and the transcription task.  If the queue fills,
  the oldest chunk is evicted so the browser is never stalled.
* **No disk persistence** — audio bytes live only in memory buffers and are
  discarded after transcription.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.audit import write_audit_log_bg
from app.db.session import get_sessionmaker
from app.services.audio_ingest import (
    CHUNK_QUEUE_MAXSIZE,
    acquire_operator_lock,
    drain_transcription,
    enqueue_chunk,
    get_audio_byte_limiter,
    release_operator_lock,
    transcription_task,
    validate_active_session,
)
from app.services.pubsub import transcript_pubsub
from app.ws.auth import authenticate_ws_operator

logger = logging.getLogger(__name__)

operator_audio_router = APIRouter()


@operator_audio_router.websocket("/operator/{session_id}/audio")
async def operator_audio_ws(websocket: WebSocket, session_id: UUID) -> None:
    """Operator audio ingest endpoint.

    Protocol
    --------
    1. Client connects with ``?token=<jwt>`` query param.
    2. Server validates JWT, session existence/status, and operator lock.
    3. Server accepts the WebSocket.
    4. Client sends binary frames containing WebM/Opus chunks (~2-3 s each).
    5. Server forwards chunks to the transcription provider via a bounded queue.
    6. Transcribed text is published as :class:`TranscriptEvent`.
    7. On disconnect the transcription task is drained and the lock is released.
    """
    # --- Auth ----------------------------------------------------------------
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        user = await authenticate_ws_operator(websocket, db)
        if user is None:
            return  # socket already closed by auth helper

        # --- Session validation ----------------------------------------------
        session = await validate_active_session(db, session_id)
        if session is None:
            await websocket.close(code=4404, reason="session not found or not active")
            return

        source_language = session.source_language
        operator_user_id = user.id

    # --- Single-operator lock ------------------------------------------------
    if not await acquire_operator_lock(session_id):
        await websocket.close(code=4409, reason="another operator is already connected")
        return

    try:
        await websocket.accept()
        logger.info(
            "operator %d connected to session %s", operator_user_id, session_id
        )
        await write_audit_log_bg(
            sessionmaker,
            action="operator.audio.connect",
            actor_user_id=operator_user_id,
            target_type="session",
            target_id=str(session_id),
        )

        # Bounded queue for backpressure between receive loop and transcription.
        chunk_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=CHUNK_QUEUE_MAXSIZE
        )

        # Ensure the pub/sub channel exists for this session.
        await transcript_pubsub.get_or_create(session_id)

        # Spawn transcription consumer as a background task.
        transcription = asyncio.create_task(
            transcription_task(session_id, source_language, chunk_queue),
            name=f"transcribe-{session_id}",
        )

        byte_limiter = await get_audio_byte_limiter()

        try:
            while True:
                data = await websocket.receive_bytes()
                if not data:
                    continue
                if not await byte_limiter.record_and_check(session_id, len(data)):
                    logger.warning(
                        "operator %d exceeded audio byte cap for session %s; closing",
                        operator_user_id,
                        session_id,
                    )
                    await websocket.close(
                        code=4429, reason="audio byte rate limit exceeded"
                    )
                    break
                enqueue_chunk(chunk_queue, data, session_id)
        except WebSocketDisconnect:
            logger.info(
                "operator %d disconnected from session %s",
                operator_user_id,
                session_id,
            )
        finally:
            await drain_transcription(chunk_queue, transcription, session_id)
    finally:
        await release_operator_lock(session_id)
        await transcript_pubsub.remove_if_empty(session_id)
        logger.info("operator lock released for session %s", session_id)
        await write_audit_log_bg(
            sessionmaker,
            action="operator.audio.disconnect",
            actor_user_id=operator_user_id,
            target_type="session",
            target_id=str(session_id),
        )


def reset_operator_locks() -> None:
    """Clear all operator locks.  Used in tests."""
    from app.services.audio_ingest import reset_operator_locks as _reset

    _reset()
