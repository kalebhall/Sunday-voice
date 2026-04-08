"""TTS audio streaming endpoint.

Listeners fetch synthesized audio for a translation segment via
``GET /api/tts/{segment_id}``.  The endpoint is read-only and anonymous
(same as the listener WebSocket).  Audio is streamed from the on-disk
cache populated by :class:`~app.services.tts.TTSService`.

Session-scoping invariant
-------------------------
The segment must belong to a session that is currently ACTIVE.  This
prevents anonymous users from enumerating sequential integer segment IDs
to harvest audio from ended or other sessions.  ``get_audio_for_segment``
performs this check internally via :meth:`TTSService.get_audio_for_segment`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response

from app.api.deps import DbSession
from app.models.segment import TranslationSegment
from app.models.session import SessionStatus

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{segment_id}")
async def get_tts_audio(segment_id: int, request: Request, db: DbSession) -> Response:
    """Stream synthesized audio for a translation segment.

    The segment must belong to an ACTIVE session — this enforces session
    isolation and prevents enumeration of audio from ended sessions.

    Returns 404 if the segment doesn't exist, the session is not active,
    TTS is disabled for the language, or synthesis hasn't completed yet.
    Returns 503 if the TTS service is not configured.
    """
    tts_service = getattr(request.app.state, "tts_service", None)
    if tts_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TTS service not available",
        )

    # Session-isolation guard: verify the segment's session is ACTIVE before
    # returning any audio.  This prevents enumeration of audio from sessions
    # the requester has no relationship to.
    seg = await db.get(TranslationSegment, segment_id)
    if seg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audio not available")

    from sqlalchemy import select
    from app.models.session import Session

    session = (
        await db.execute(select(Session).where(Session.id == seg.session_id))
    ).scalar_one_or_none()
    if session is None or session.status != SessionStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audio not available")

    audio, content_type = await tts_service.get_audio_for_segment(segment_id)

    if audio is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="audio not available",
        )

    return Response(
        content=audio,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Disposition": f'inline; filename="segment_{segment_id}.mp3"',
        },
    )
