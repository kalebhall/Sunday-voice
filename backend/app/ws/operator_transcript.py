"""WebSocket endpoint: operator text-only transcript ingest.

``/ws/operator/{session_id}/transcript?token=<jwt>``

Used when the operator selects the **Browser (Web Speech API)** transcription
mode.  The browser performs on-device speech recognition and sends the
resulting text segments as JSON frames; this endpoint pushes them into the
same in-process pub/sub pipeline used by the Whisper and WebRTC audio paths.

Protocol
--------
1. Client connects with ``?token=<jwt>`` query param.
2. Server validates JWT, session existence/status, and the single-operator lock.
3. Server accepts the WebSocket.
4. Client sends JSON text frames: ``{"text": "transcribed words", "language": "en"}``.
   The ``language`` field is optional; when omitted the session source language
   is used.
5. Server validates, sequences, and publishes a :class:`TranscriptEvent`.
6. On disconnect the operator lock is released.

Invariants enforced (same as audio endpoint):
* **Operator JWT** — ``operator`` or ``admin`` role required.
* **Single-operator-per-session lock** — shared with the audio ingest path so
  only one ingest connection (audio *or* text) can be open per session at a time.
* **Text length cap** — frames whose ``text`` field exceeds 2 000 characters
  are silently dropped to prevent runaway DB row sizes.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.audit import write_audit_log_bg
from app.db.session import get_sessionmaker
from app.services.audio_ingest import (
    acquire_operator_lock,
    release_operator_lock,
    validate_active_session,
)
from app.services.pubsub import TranscriptEvent, transcript_pubsub
from app.ws.auth import authenticate_ws_operator

logger = logging.getLogger(__name__)

operator_transcript_router = APIRouter()

# Maximum characters accepted per text frame.
_MAX_TEXT_CHARS = 2_000


@operator_transcript_router.websocket("/operator/{session_id}/transcript")
async def operator_transcript_ws(websocket: WebSocket, session_id: UUID) -> None:
    """Operator text-only ingest: receives pre-transcribed segments from the browser."""
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

    # --- Single-operator lock (shared with audio endpoint) -------------------
    if not await acquire_operator_lock(session_id):
        await websocket.close(code=4409, reason="another operator is already connected")
        return

    # Grab the translation fanout from app state (may be None if Google
    # Cloud credentials are not configured).
    translation_fanout = getattr(websocket.app.state, "translation_fanout", None)

    try:
        await websocket.accept()
        logger.info(
            "operator %d connected (web-speech transcript) to session %s",
            operator_user_id,
            session_id,
        )
        await write_audit_log_bg(
            sessionmaker,
            action="operator.transcript.connect",
            actor_user_id=operator_user_id,
            target_type="session",
            target_id=str(session_id),
        )

        # Start the translation fanout for this session so TranscriptEvents
        # are translated and published to Redis for listener delivery.
        if translation_fanout is not None:
            await translation_fanout.start(session_id)

        # Ensure the pub/sub channel exists for this session.
        await transcript_pubsub.get_or_create(session_id)

        sequence = 0

        try:
            while True:
                raw = await websocket.receive_text()

                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    logger.debug(
                        "operator transcript: non-JSON frame from session %s; ignored",
                        session_id,
                    )
                    continue

                text = payload.get("text", "")
                if not isinstance(text, str):
                    continue
                text = text.strip()
                if not text:
                    continue
                if len(text) > _MAX_TEXT_CHARS:
                    logger.debug(
                        "operator transcript: oversized frame (%d chars) from session %s; dropped",
                        len(text),
                        session_id,
                    )
                    continue

                # Respect an explicit language field if present and non-empty.
                language = payload.get("language", "")
                if not isinstance(language, str) or not language.strip():
                    language = source_language

                sequence += 1
                event = TranscriptEvent(
                    session_id=session_id,
                    sequence=sequence,
                    language=language,
                    text=text,
                )
                await transcript_pubsub.publish(event)
                logger.debug(
                    "transcript seq=%d session=%s len=%d (web-speech)",
                    sequence,
                    session_id,
                    len(text),
                )

        except WebSocketDisconnect:
            logger.info(
                "operator %d disconnected (web-speech transcript) from session %s",
                operator_user_id,
                session_id,
            )
        finally:
            if translation_fanout is not None:
                await translation_fanout.stop(session_id)
    finally:
        await release_operator_lock(session_id)
        await transcript_pubsub.remove_if_empty(session_id)
        logger.info(
            "operator transcript lock released for session %s", session_id
        )
        await write_audit_log_bg(
            sessionmaker,
            action="operator.transcript.disconnect",
            actor_user_id=operator_user_id,
            target_type="session",
            target_id=str(session_id),
        )
