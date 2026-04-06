"""In-process asyncio pub/sub for transcript segment events.

Provides a lightweight fan-out mechanism so the WebSocket audio ingest handler
can publish TranscriptSegment events that downstream consumers (translation
pipeline, listener fan-out) subscribe to without tight coupling.

This is intentionally in-process.  For multi-worker fan-out the architecture
calls for Redis pub/sub (post-MVP); this module provides the single-worker
building block and will sit behind the same publish/subscribe interface.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """Normalized transcript segment emitted by the transcription provider."""

    session_id: UUID
    sequence: int
    language: str
    text: str
    start_ms: int | None = None
    end_ms: int | None = None


class SessionPubSub:
    """Per-session, in-process pub/sub backed by :class:`asyncio.Queue`.

    Subscribers receive a bounded queue.  If a subscriber falls behind and its
    queue is full the oldest event is dropped (back-pressure policy) so a slow
    consumer never blocks the publisher or other subscribers.
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        self._subscribers: dict[int, asyncio.Queue[TranscriptEvent]] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def subscribe(self) -> tuple[int, asyncio.Queue[TranscriptEvent]]:
        """Add a subscriber.  Returns ``(sub_id, queue)``."""
        async with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            q: asyncio.Queue[TranscriptEvent] = asyncio.Queue(maxsize=self._maxsize)
            self._subscribers[sub_id] = q
            return sub_id, q

    async def unsubscribe(self, sub_id: int) -> None:
        async with self._lock:
            self._subscribers.pop(sub_id, None)

    async def publish(self, event: TranscriptEvent) -> None:
        """Fan out *event* to all current subscribers.

        If a subscriber queue is full the oldest item is evicted so the
        publisher is never blocked.
        """
        async with self._lock:
            for sid, q in self._subscribers.items():
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop oldest to maintain backpressure.
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        logger.warning(
                            "subscriber %d queue still full after eviction", sid
                        )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class TranscriptPubSubRegistry:
    """Global registry mapping session IDs to their :class:`SessionPubSub`.

    Lazily creates a pub/sub instance the first time a session is referenced,
    and cleans up when no subscribers remain.
    """

    def __init__(self, queue_maxsize: int = 256) -> None:
        self._sessions: dict[UUID, SessionPubSub] = {}
        self._lock = asyncio.Lock()
        self._queue_maxsize = queue_maxsize

    async def get_or_create(self, session_id: UUID) -> SessionPubSub:
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionPubSub(
                    maxsize=self._queue_maxsize
                )
            return self._sessions[session_id]

    async def remove_if_empty(self, session_id: UUID) -> None:
        """Remove the pub/sub for *session_id* if it has no subscribers."""
        async with self._lock:
            ps = self._sessions.get(session_id)
            if ps is not None and ps.subscriber_count == 0:
                del self._sessions[session_id]

    async def publish(self, event: TranscriptEvent) -> None:
        """Publish directly by session_id (convenience wrapper)."""
        async with self._lock:
            ps = self._sessions.get(event.session_id)
        if ps is not None:
            await ps.publish(event)

    @property
    def active_sessions(self) -> list[UUID]:
        return list(self._sessions.keys())


# Module-level singleton.  Imported by the app lifespan and WS handlers.
transcript_pubsub = TranscriptPubSubRegistry()
