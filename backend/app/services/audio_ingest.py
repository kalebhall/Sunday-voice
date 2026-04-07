"""Shared audio-ingest helpers used by both WebSocket and WebRTC transports.

Provides the single-operator lock, session validation, chunk queue helpers,
and the transcription task that publishes :class:`TranscriptEvent` objects to
the in-process pub/sub.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Session, SessionStatus
from app.providers.whisper import WhisperAPIProvider
from app.services.pubsub import TranscriptEvent, transcript_pubsub

logger = logging.getLogger(__name__)

# Receive-side backpressure: max queued chunks before eviction.
CHUNK_QUEUE_MAXSIZE = 30  # ~60-90 s of audio at 2-3 s chunks

# Per-session lock: maps session_id → True while an operator is connected.
_active_operators: dict[UUID, bool] = {}
_active_operators_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Operator audio byte rate limiter
# ---------------------------------------------------------------------------


class AudioByteRateLimiter:
    """Rolling per-minute byte counter, keyed by session UUID.

    Tracks how many bytes an operator has sent in the past 60 seconds.
    ``record_and_check`` both records the new chunk and returns whether the
    running total is still within the configured cap.
    """

    _WINDOW_SECONDS = 60.0

    def __init__(self, max_bytes_per_minute: int) -> None:
        self._max_bytes = max_bytes_per_minute
        # Maps session_id → deque of (monotonic_timestamp, byte_count) pairs.
        self._windows: dict[UUID, deque[tuple[float, int]]] = {}
        self._lock = asyncio.Lock()

    async def record_and_check(self, session_id: UUID, num_bytes: int) -> bool:
        """Record *num_bytes* for *session_id*.  Returns True if within cap."""
        now = time.monotonic()
        cutoff = now - self._WINDOW_SECONDS
        async with self._lock:
            window = self._windows.setdefault(session_id, deque())
            # Evict entries older than the rolling window.
            while window and window[0][0] <= cutoff:
                window.popleft()
            total = sum(b for _, b in window) + num_bytes
            if total > self._max_bytes:
                return False
            window.append((now, num_bytes))
            return True

    def reset(self, session_id: UUID | None = None) -> None:
        """Clear state.  Used in tests."""
        if session_id is None:
            self._windows.clear()
        else:
            self._windows.pop(session_id, None)


# Module-level singleton, initialised lazily from settings.
_audio_byte_limiter: AudioByteRateLimiter | None = None
_audio_byte_limiter_lock = asyncio.Lock()


async def get_audio_byte_limiter() -> AudioByteRateLimiter:
    """Return the process-wide audio byte limiter, creating it lazily."""
    global _audio_byte_limiter
    if _audio_byte_limiter is None:
        async with _audio_byte_limiter_lock:
            if _audio_byte_limiter is None:
                settings = get_settings()
                _audio_byte_limiter = AudioByteRateLimiter(
                    max_bytes_per_minute=settings.operator_audio_max_bytes_per_minute
                )
    return _audio_byte_limiter


def reset_audio_byte_limiter() -> None:
    """Discard the cached limiter.  Used in tests."""
    global _audio_byte_limiter
    _audio_byte_limiter = None


async def acquire_operator_lock(session_id: UUID) -> bool:
    """Try to claim the single-operator slot.  Returns True on success."""
    async with _active_operators_lock:
        if _active_operators.get(session_id):
            return False
        _active_operators[session_id] = True
        return True


async def release_operator_lock(session_id: UUID) -> None:
    async with _active_operators_lock:
        _active_operators.pop(session_id, None)


async def validate_active_session(db: AsyncSession, session_id: UUID) -> Session | None:
    """Load the session and verify it is active."""
    session = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if session is None:
        return None
    if session.status != SessionStatus.ACTIVE:
        return None
    return session


async def chunk_generator(
    queue: asyncio.Queue[bytes | None],
) -> AsyncIterator[bytes]:
    """Yield audio chunks from *queue* until a sentinel ``None`` is received."""
    while True:
        chunk = await queue.get()
        if chunk is None:
            return
        yield chunk


def enqueue_chunk(queue: asyncio.Queue[bytes | None], data: bytes, session_id: UUID) -> None:
    """Put *data* into *queue* with backpressure (evict oldest if full)."""
    try:
        queue.put_nowait(data)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("chunk queue still full after eviction, session %s", session_id)


async def transcription_task(
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
            chunk_generator(queue),
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


async def drain_transcription(
    queue: asyncio.Queue[bytes | None],
    task: asyncio.Task[None],
    session_id: UUID,
    drain_timeout: float = 30.0,
) -> None:
    """Signal end-of-stream, wait for the transcription task to finish."""
    await queue.put(None)
    try:
        await asyncio.wait_for(task, timeout=drain_timeout)
    except TimeoutError:
        logger.warning("transcription drain timed out for session %s", session_id)
        task.cancel()
        with asyncio.suppress(asyncio.CancelledError):
            await task


def reset_operator_locks() -> None:
    """Clear all operator locks.  Used in tests."""
    _active_operators.clear()
