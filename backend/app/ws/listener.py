"""WebSocket endpoint: anonymous listener fan-out.

``/ws/listen/{session_code}/{lang}?after_seq=N``

Streams translated segments to anonymous listeners.  On connect the server
sends a scrollback of the last N segments (configurable), then streams new
segments in real-time via Redis pub/sub.  A periodic heartbeat keeps the
connection alive through proxies and load balancers.

Invariants enforced:

* **Anonymous / read-only** — no authentication required.  The listener
  receives data but never sends meaningful frames; any text frame from the
  client is silently ignored.
* **Session scoped** — the session is looked up by join_code *or* join_slug
  and must be in ACTIVE status.
* **Per-IP connection cap** — a single IP address may not exceed
  ``LISTENER_MAX_CONNECTIONS_PER_IP`` concurrent WebSocket connections.
* **Per-session connection cap** — a single session may not have more than
  ``LISTENER_MAX_CONNECTIONS_PER_SESSION`` concurrent listener connections.
* **Reconnect-friendly** — every segment carries a monotonically increasing
  ``seq`` (the TranslationSegment's source transcript sequence number).
  Clients may pass ``?after_seq=N`` to skip scrollback segments they already
  have, enabling seamless reconnection without duplicates.
* **Heartbeat** — a JSON ``{"type": "heartbeat"}`` message is sent every
  ``LISTENER_HEARTBEAT_SECONDS`` to keep the connection alive.
* **Kill-switch** — when an operator ends a session the server publishes a
  ``{"type": "session_ended"}`` control message that immediately closes all
  active listener connections for that session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.metrics import connected_listeners, segment_pipeline_duration_seconds
from app.db.session import get_sessionmaker
from app.models.segment import TranslationSegment
from app.models.session import Session, SessionLanguage, SessionStatus
from app.services.listener_connections import listener_connections

logger = logging.getLogger(__name__)

listener_router = APIRouter()

# Redis channel suffix used for session control messages (kill-switch).
_CONTROL_SUFFIX = ":control"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _lookup_active_session(
    session_code: str,
) -> tuple[UUID, str] | None:
    """Resolve *session_code* (join_code or join_slug) to (session_id, source_language).

    Returns ``None`` if the session does not exist or is not ACTIVE.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        stmt = (
            select(Session)
            .options(selectinload(Session.languages))
            .where(
                (Session.join_code == session_code) | (Session.join_slug == session_code)
            )
        )
        session = (await db.execute(stmt)).scalar_one_or_none()
        if session is None or session.status != SessionStatus.ACTIVE:
            return None

        return session.id, session.source_language


async def _validate_language(session_code: str, lang: str) -> bool:
    """Check that *lang* is an enabled target language (or source) for the session."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        stmt = select(Session).options(selectinload(Session.languages)).where(
            (Session.join_code == session_code) | (Session.join_slug == session_code)
        )
        session = (await db.execute(stmt)).scalar_one_or_none()
        if session is None:
            return False
        enabled = {sl.language_code for sl in session.languages}
        enabled.add(session.source_language)
        return lang in enabled


async def _fetch_scrollback(
    session_id: UUID,
    lang: str,
    limit: int,
    after_seq: int,
) -> list[dict]:
    """Load the most recent translated segments from the database.

    Returns up to *limit* segments whose source transcript sequence is
    greater than *after_seq*, ordered oldest-first so the client can
    append them in order.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        stmt = (
            select(TranslationSegment)
            .where(
                TranslationSegment.session_id == session_id,
                TranslationSegment.language_code == lang,
            )
            .join(
                TranslationSegment.transcript_segment,
            )
        )

        # Import here to avoid circular — only needed for the join filter.
        from app.models.segment import TranscriptSegment

        if after_seq > 0:
            stmt = stmt.where(TranscriptSegment.sequence > after_seq)

        stmt = (
            stmt.order_by(TranscriptSegment.sequence.desc())
            .limit(limit)
        )

        rows = (await db.execute(stmt)).scalars().all()

    # Reverse so oldest is first.
    segments = []
    for row in reversed(list(rows)):
        segments.append(
            {
                "type": "segment",
                "seq": row.transcript_segment.sequence,
                "language": row.language_code,
                "text": row.text,
                "source_language": row.transcript_segment.language,
                "segment_id": row.id,
                "tts_url": f"/api/tts/{row.id}",
            }
        )
    return segments


def _client_ip(websocket: WebSocket) -> str:
    """Extract client IP, respecting X-Forwarded-For from a trusted reverse proxy."""
    forwarded = websocket.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if websocket.client:
        return websocket.client.host
    return "unknown"


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@listener_router.websocket("/listen/{session_code}/{lang}")
async def listener_ws(
    websocket: WebSocket,
    session_code: str,
    lang: str,
) -> None:
    """Anonymous listener subscribes to translated segments for a session + language.

    Query params
    ------------
    after_seq : int, optional
        If provided, scrollback will only include segments with sequence > after_seq.
        Useful for reconnection — the client tells the server what it already has.
    """
    settings = get_settings()
    ip = _client_ip(websocket)

    # --- Session + language validation (before holding a connection slot) ----
    result = await _lookup_active_session(session_code)
    if result is None:
        await websocket.close(code=4404, reason="session not found or not active")
        return

    session_id, source_language = result

    if not await _validate_language(session_code, lang):
        await websocket.close(code=4400, reason="language not enabled for this session")
        return

    # --- Connection caps (holds slots for duration of connection) ------------
    session_key = str(session_id)
    async with listener_connections.track(ip, session_key) as (allowed, reason):
        if not allowed:
            await websocket.close(code=4429, reason=reason)
            logger.warning(
                "listener connection rejected: ip=%s session=%s reason=%r",
                ip,
                session_id,
                reason,
            )
            return

        # --- Accept ----------------------------------------------------------
        await websocket.accept()
        connected_listeners.inc()
        logger.info(
            "listener connected: session=%s lang=%s ip=%s",
            session_id, lang, ip,
        )

        # Parse reconnect cursor from query params.
        after_seq = 0
        raw = websocket.query_params.get("after_seq")
        if raw is not None:
            try:
                after_seq = max(0, int(raw))
            except (ValueError, TypeError):
                pass

        # --- Scrollback ------------------------------------------------------
        scrollback_limit = getattr(settings, "listener_scrollback_limit", 50)
        scrollback = await _fetch_scrollback(session_id, lang, scrollback_limit, after_seq)

        await websocket.send_json(
            {
                "type": "scrollback",
                "segments": scrollback,
                "count": len(scrollback),
            }
        )

        last_seq = after_seq
        if scrollback:
            last_seq = max(last_seq, scrollback[-1]["seq"])

        # --- Redis pub/sub subscription --------------------------------------
        redis_url = settings.redis_url
        redis = Redis.from_url(redis_url, decode_responses=True)
        lang_channel = f"session:{session_id}:lang:{lang}"
        control_channel = f"session:{session_id}{_CONTROL_SUFFIX}"
        pubsub = redis.pubsub()

        try:
            await pubsub.subscribe(lang_channel, control_channel)

            heartbeat_interval = getattr(settings, "listener_heartbeat_seconds", 15)
            recv_task: asyncio.Task[None] | None = None

            async def _drain_client() -> None:
                """Read and discard any client frames (read-only endpoint)."""
                try:
                    while True:
                        await websocket.receive_text()
                except WebSocketDisconnect:
                    raise
                except Exception:
                    pass

            recv_task = asyncio.create_task(
                _drain_client(), name=f"listener-drain-{session_id}-{lang}"
            )

            while True:
                try:
                    msg = await asyncio.wait_for(
                        pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0
                        ),
                        timeout=heartbeat_interval,
                    )
                except asyncio.TimeoutError:
                    msg = None

                # Check if the client disconnected.
                if recv_task.done():
                    break

                if msg is not None and msg["type"] == "message":
                    # --- Kill-switch: session ended --------------------------
                    if msg["channel"] == control_channel:
                        try:
                            ctrl = json.loads(msg["data"])
                        except (json.JSONDecodeError, TypeError):
                            ctrl = {}
                        if ctrl.get("type") == "session_ended":
                            logger.info(
                                "kill-switch received: closing listener session=%s lang=%s ip=%s",
                                session_id, lang, ip,
                            )
                            await websocket.send_json({"type": "session_ended"})
                            await websocket.close(
                                code=4410, reason="session ended by operator"
                            )
                            break
                        continue

                    # --- Normal segment message ------------------------------
                    try:
                        payload = json.loads(msg["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue

                    seq = payload.get("sequence", 0)
                    if seq <= last_seq:
                        # Already sent in scrollback — deduplicate.
                        continue
                    last_seq = seq

                    # Observe end-to-end pipeline latency when available.
                    published_at = payload.get("published_at")
                    if published_at is not None:
                        try:
                            segment_pipeline_duration_seconds.observe(
                                time.monotonic() - float(published_at)
                            )
                        except (TypeError, ValueError):
                            pass

                    await websocket.send_json(
                        {
                            "type": "segment",
                            "seq": seq,
                            "language": payload.get("language", lang),
                            "text": payload.get("text", ""),
                            "source_language": payload.get("source_language", ""),
                            "segment_id": payload.get("segment_id"),
                            "tts_url": payload.get("tts_url"),
                        }
                    )
                else:
                    # No message within heartbeat window — send keepalive.
                    await websocket.send_json({"type": "heartbeat"})

        except WebSocketDisconnect:
            logger.info(
                "listener disconnected: session=%s lang=%s ip=%s",
                session_id, lang, ip,
            )
        except Exception:
            logger.exception(
                "listener error: session=%s lang=%s ip=%s",
                session_id, lang, ip,
            )
        finally:
            connected_listeners.dec()
            if recv_task is not None and not recv_task.done():
                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, Exception):
                    pass
            await pubsub.unsubscribe(lang_channel, control_channel)
            await pubsub.aclose()
            await redis.aclose()
            logger.info(
                "listener cleanup complete: session=%s lang=%s ip=%s",
                session_id, lang, ip,
            )
