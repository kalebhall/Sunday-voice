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
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models import Session, SessionStatus
from app.providers.whisper import WhisperAPIProvider
from app.services.pubsub import TranscriptEvent, transcript_pubsub
from app.ws.auth import authenticate_ws_operator

logger = logging.getLogger(__name__)

operator_audio_router = APIRouter()

# Per-session lock: maps session_id → True while an operator is connected.
_active_operators: dict[UUID, bool] = {}
_active_operators_lock = asyncio.Lock()

# Receive-side backpressure: max queued chunks before eviction.
_CHUNK_QUEUE_MAXSIZE = 30  # ~60-90 s of audio at 2-3 s chunks


async def _acquire_operator_lock(session_id: UUID) -> bool:
    """Try to claim the single-operator slot.  Returns True on success."""
    async with _active_operators_lock:
        if _active_operators.get(session_id):
            return False
        _active_operators[session_id] = True
        return True


async def _release_operator_lock(session_id: UUID) -> None:
    async with _active_operators_lock:
        _active_operators.pop(session_id, None)


async def _validate_session(db: AsyncSession, session_id: UUID) -> Session | None:
    """Load the session and verify it is active."""
    session = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if session is None:
        return None
    if session.status != SessionStatus.ACTIVE:
        return None
    return session


async def _chunk_generator(
    queue: asyncio.Queue[bytes | None],
) -> AsyncIterator[bytes]:
    """Yield audio chunks from *queue* until a sentinel ``None`` is received."""
    while True:
        chunk = await queue.get()
        if chunk is None:
            return
        yield chunk


async def _transcription_task(
    session_id: UUID,
    source_language: str,
    queue: asyncio.Queue[bytes | None],
) -> None:
    """Consume audio chunks, run transcription, publish events."""
    settings = get_settings()
    provider = WhisperAPIProvider(
        api_key=settings.openai_api_key,
        model=settings.whisper_model,
    )

    sequence = 0
    try:
        async for text in provider.transcribe_stream(
            _chunk_generator(queue),
            source_language=source_language,
        ):
            if not text or not text.strip():
                continue
            sequence += 1
            event = TranscriptEvent(
                session_id=session_id,
                sequence=sequence,
                language=source_language,
                text=text.strip(),
            )
            await transcript_pubsub.publish(event)
            logger.debug(
                "published transcript seq=%d session=%s len=%d",
                sequence,
                session_id,
                len(event.text),
            )
    except Exception:
        logger.exception("transcription task failed for session %s", session_id)


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
        session = await _validate_session(db, session_id)
        if session is None:
            await websocket.close(code=4404, reason="session not found or not active")
            return

        source_language = session.source_language
        operator_user_id = user.id

    # --- Single-operator lock ------------------------------------------------
    if not await _acquire_operator_lock(session_id):
        await websocket.close(code=4409, reason="another operator is already connected")
        return

    try:
        await websocket.accept()
        logger.info(
            "operator %d connected to session %s", operator_user_id, session_id
        )

        # Bounded queue for backpressure between receive loop and transcription.
        chunk_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=_CHUNK_QUEUE_MAXSIZE
        )

        # Ensure the pub/sub channel exists for this session.
        await transcript_pubsub.get_or_create(session_id)

        # Spawn transcription consumer as a background task.
        transcription = asyncio.create_task(
            _transcription_task(session_id, source_language, chunk_queue),
            name=f"transcribe-{session_id}",
        )

        try:
            while True:
                data = await websocket.receive_bytes()
                if not data:
                    continue
                try:
                    chunk_queue.put_nowait(data)
                except asyncio.QueueFull:
                    # Backpressure: drop oldest chunk to make room.
                    try:
                        chunk_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        chunk_queue.put_nowait(data)
                    except asyncio.QueueFull:
                        logger.warning(
                            "chunk queue still full after eviction, session %s",
                            session_id,
                        )
        except WebSocketDisconnect:
            logger.info(
                "operator %d disconnected from session %s",
                operator_user_id,
                session_id,
            )
        finally:
            # Signal end-of-stream to the transcription task and wait for it
            # to drain remaining buffered audio.
            await chunk_queue.put(None)
            try:
                await asyncio.wait_for(transcription, timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "transcription drain timed out for session %s", session_id
                )
                transcription.cancel()
                with asyncio.suppress(asyncio.CancelledError):
                    await transcription
    finally:
        await _release_operator_lock(session_id)
        await transcript_pubsub.remove_if_empty(session_id)
        logger.info("operator lock released for session %s", session_id)


def reset_operator_locks() -> None:
    """Clear all operator locks.  Used in tests."""
    _active_operators.clear()
