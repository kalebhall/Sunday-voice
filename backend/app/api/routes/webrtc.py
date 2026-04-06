"""WebRTC transport: SDP offer/answer exchange and server-side audio decoding.

``POST /api/sessions/{session_id}/webrtc/offer``

The operator's browser establishes a WebRTC PeerConnection and sends an SDP
offer.  The server responds with an SDP answer.  Once the ICE connection is
established, audio frames are decoded server-side, windowed into ~2-3 s PCM
segments, re-encoded to WAV (Whisper-acceptable), and fed into the same
:func:`transcription_task` pipeline used by the WebSocket chunked transport.

Invariants match the WebSocket transport:
* Operator JWT required (Bearer token).
* Single-operator-per-session lock.
* No disk persistence — audio lives only in memory.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from typing import Annotated
from uuid import UUID

from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import DbSession, require_role
from app.models import User
from app.services.audio_ingest import (
    CHUNK_QUEUE_MAXSIZE,
    acquire_operator_lock,
    drain_transcription,
    enqueue_chunk,
    release_operator_lock,
    transcription_task,
    validate_active_session,
)
from app.services.pubsub import transcript_pubsub

logger = logging.getLogger(__name__)

router = APIRouter()

_require_operator = require_role("admin", "operator")
OperatorUser = Annotated[User, Depends(_require_operator)]

# Target window size for PCM accumulation before encoding to WAV.
_WINDOW_SECONDS = 2.5

# aiortc decodes to 48 kHz stereo by default; we resample to 16 kHz mono
# which is what Whisper prefers.
_TARGET_SAMPLE_RATE = 16_000
_TARGET_CHANNELS = 1

# Active peer connections keyed by session_id for lifecycle management.
_peer_connections: dict[UUID, RTCPeerConnection] = {}


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class SDPOffer(BaseModel):
    sdp: str
    type: str = "offer"


class SDPAnswer(BaseModel):
    sdp: str
    type: str = "answer"


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------


def _pcm_to_wav(pcm_data: bytes, sample_rate: int, channels: int) -> bytes:
    """Encode raw 16-bit PCM samples into a WAV byte string."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def _resample_frame(frame: object) -> bytes:
    """Resample an av.AudioFrame to 16 kHz mono s16 PCM and return raw bytes.

    aiortc delivers decoded ``av.AudioFrame`` objects.  We use the PyAV
    resampler built into the frame to convert to Whisper's preferred format.
    """
    import av.audio.resampler

    resampler = av.audio.resampler.AudioResampler(
        format="s16",
        layout="mono",
        rate=_TARGET_SAMPLE_RATE,
    )
    resampled = resampler.resample(frame)
    out = b""
    for rf in resampled:
        out += bytes(rf.planes[0])
    return out


# ---------------------------------------------------------------------------
# Track consumer: reads audio frames, windows, encodes, feeds chunk queue
# ---------------------------------------------------------------------------


async def _consume_audio_track(
    track: MediaStreamTrack,
    session_id: UUID,
    chunk_queue: asyncio.Queue[bytes | None],
) -> None:
    """Read frames from *track*, window into ~2-3 s PCM, encode WAV, enqueue."""
    pcm_buffer = bytearray()
    target_bytes = int(_WINDOW_SECONDS * _TARGET_SAMPLE_RATE * _TARGET_CHANNELS * 2)  # 16-bit

    try:
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                # Track ended (peer disconnected or track stopped).
                break

            pcm = _resample_frame(frame)
            pcm_buffer.extend(pcm)

            if len(pcm_buffer) >= target_bytes:
                wav_bytes = _pcm_to_wav(
                    bytes(pcm_buffer[:target_bytes]),
                    _TARGET_SAMPLE_RATE,
                    _TARGET_CHANNELS,
                )
                enqueue_chunk(chunk_queue, wav_bytes, session_id)
                pcm_buffer = pcm_buffer[target_bytes:]
    except Exception:
        logger.exception("audio track consumer failed for session %s", session_id)
    finally:
        # Flush any remaining PCM as a final (shorter) WAV segment.
        if pcm_buffer:
            wav_bytes = _pcm_to_wav(
                bytes(pcm_buffer),
                _TARGET_SAMPLE_RATE,
                _TARGET_CHANNELS,
            )
            enqueue_chunk(chunk_queue, wav_bytes, session_id)


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


async def _on_connection_closed(
    pc: RTCPeerConnection,
    session_id: UUID,
    chunk_queue: asyncio.Queue[bytes | None],
    task: asyncio.Task[None],
) -> None:
    """Clean up after the peer connection closes."""
    await drain_transcription(chunk_queue, task, session_id)
    await release_operator_lock(session_id)
    await transcript_pubsub.remove_if_empty(session_id)
    _peer_connections.pop(session_id, None)
    logger.info("webrtc connection closed for session %s", session_id)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/{session_id}/webrtc/offer",
    response_model=SDPAnswer,
    status_code=status.HTTP_200_OK,
)
async def webrtc_offer(
    session_id: UUID,
    offer: SDPOffer,
    db: DbSession,
    user: OperatorUser,
) -> SDPAnswer:
    """Exchange SDP offer/answer to establish a WebRTC audio connection.

    The operator sends an SDP offer; the server creates a PeerConnection,
    sets the remote description, generates an answer, and returns it.  Once
    the ICE connection is established the server decodes the incoming audio
    track, windows it into ~2-3 s PCM frames, re-encodes to WAV, and feeds
    the standard transcription pipeline.
    """
    # --- Session validation --------------------------------------------------
    session = await validate_active_session(db, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="session not found or not active",
        )

    source_language = session.source_language

    # --- Single-operator lock ------------------------------------------------
    if not await acquire_operator_lock(session_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="another operator is already connected",
        )

    try:
        pc = RTCPeerConnection()
        _peer_connections[session_id] = pc

        chunk_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=CHUNK_QUEUE_MAXSIZE,
        )

        # Ensure the pub/sub channel exists for this session.
        await transcript_pubsub.get_or_create(session_id)

        # Spawn the transcription consumer.
        tx_task = asyncio.create_task(
            transcription_task(session_id, source_language, chunk_queue),
            name=f"transcribe-webrtc-{session_id}",
        )

        # Hold a reference to the audio consumer task so it isn't GC'd.
        _audio_tasks: list[asyncio.Task[None]] = []

        @pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind != "audio":
                return
            logger.info(
                "webrtc audio track received for session %s", session_id
            )

            task = asyncio.create_task(
                _consume_audio_track(track, session_id, chunk_queue),
                name=f"webrtc-audio-{session_id}",
            )
            _audio_tasks.append(task)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            logger.info(
                "webrtc connection state: %s for session %s",
                pc.connectionState,
                session_id,
            )
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await _on_connection_closed(pc, session_id, chunk_queue, tx_task)

        # --- SDP exchange ----------------------------------------------------
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer.sdp, type=offer.type)
        )
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return SDPAnswer(
            sdp=pc.localDescription.sdp,
            type=pc.localDescription.type,
        )
    except Exception:
        # If anything fails during setup, release the lock and clean up.
        await release_operator_lock(session_id)
        _peer_connections.pop(session_id, None)
        raise
