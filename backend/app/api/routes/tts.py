"""TTS audio streaming endpoint.

Listeners fetch synthesized audio for a translation segment via
``GET /api/tts/{segment_id}``.  The endpoint is read-only and anonymous
(same as the listener WebSocket).  Audio is streamed from the on-disk
cache populated by :class:`~app.services.tts.TTSService`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{segment_id}")
async def get_tts_audio(segment_id: int, request: Request) -> Response:
    """Stream synthesized audio for a translation segment.

    Returns the cached MP3 (or other encoding) for the given segment ID.
    Returns 404 if the segment doesn't exist, TTS is disabled for the
    language, or synthesis hasn't completed yet.  Returns 503 if the TTS
    service is not configured.
    """
    tts_service = getattr(request.app.state, "tts_service", None)
    if tts_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TTS service not available",
        )

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
