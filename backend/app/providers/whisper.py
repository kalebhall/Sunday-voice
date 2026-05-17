"""OpenAI transcription provider (Whisper / gpt-4o-transcribe family).

Audio is transcribed using a **sliding overlap window**: rather than cutting
the stream into tiny independent slices, the provider keeps a rolling buffer
of the most recent audio and re-transcribes a long window every few seconds.
Each window overlaps the previous one heavily, so the model always sees full
sentence context (the single biggest driver of transcription accuracy).  The
overlapping text is de-duplicated before being yielded, so callers still see a
clean incremental stream of new transcript text.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx

from app.core.metrics import (
    provider_errors_total,
    segment_transcription_duration_seconds,
)

if TYPE_CHECKING:
    from app.providers.base import CostMeter

logger = logging.getLogger(__name__)

# Defaults -------------------------------------------------------------------
_DEFAULT_TIMEOUT_S = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 1.0  # 1s, 2s, 4s

# WebM/Opus from MediaRecorder runs at roughly this byte rate.  Used to turn
# the byte-based window/slide settings into human-readable durations in logs.
_WEBM_BYTES_PER_SECOND = 16_000

# Sliding-window defaults (in bytes of WebM/Opus audio, ~16 KB/s).
#   window ≈ 15 s of context per API call
#   slide  ≈ 3 s of new audio between calls (also the added latency)
_DEFAULT_WINDOW_BYTES = 240_000
_DEFAULT_SLIDE_BYTES = 48_000

# WebM EBML ID for a Cluster element.  Everything in the bitstream before the
# first Cluster is the "initialization segment" (EBML header + Segment element
# containing SeekHead, Info, and Tracks).  The model needs this header to parse
# the audio, but MediaRecorder only includes it in the very first chunk.
_WEBM_CLUSTER_ID = b"\x1f\x43\xb6\x75"


def _webm_init_end(data: bytes) -> int:
    """Return the byte offset where the WebM initialization segment ends.

    Searches for the first Cluster element (``\\x1f\\x43\\xb6\\x75``) and
    returns its offset.  If no Cluster is found the entire buffer is treated
    as initialization data (unusual, but safe).
    """
    idx = data.find(_WEBM_CLUSTER_ID)
    return idx if idx != -1 else len(data)


def _normalize_word(word: str) -> str:
    """Lowercase and strip punctuation so overlap matching is robust."""
    return re.sub(r"[^\w]", "", word.lower())


def _rfind_subsequence(haystack: list[str], needle: list[str]) -> int:
    """Return the start index of the *last* contiguous match of *needle*.

    Returns ``-1`` if *needle* does not appear in *haystack*.
    """
    if not needle or len(needle) > len(haystack):
        return -1
    for start in range(len(haystack) - len(needle), -1, -1):
        if haystack[start : start + len(needle)] == needle:
            return start
    return -1


class _OverlapDeduplicator:
    """Strips text already emitted by previous overlapping windows.

    Consecutive sliding windows share most of their audio, so each new
    transcript repeats text we have already published.  We anchor on the tail
    of the previously-committed words and emit only what follows that anchor in
    the new window.  The anchor length shrinks on a miss to tolerate the small
    word-level differences the model produces when re-transcribing the same
    audio.
    """

    _MAX_ANCHOR = 12
    _MIN_ANCHOR = 3
    _HISTORY_CAP = 300

    def __init__(self) -> None:
        self._committed: list[str] = []  # normalized words emitted so far

    def next_segment(self, text: str) -> str:
        """Return only the words in *text* not already emitted."""
        words = text.split()
        if not words:
            return ""
        norm = [_normalize_word(w) for w in words]

        if not self._committed:
            self._commit(norm)
            return text

        emit_from = self._emit_index(norm)
        if emit_from >= len(words):
            return ""  # window contained no new speech
        self._commit(norm[emit_from:])
        return " ".join(words[emit_from:])

    def _emit_index(self, norm: list[str]) -> int:
        max_anchor = min(self._MAX_ANCHOR, len(self._committed))
        for anchor_len in range(max_anchor, self._MIN_ANCHOR - 1, -1):
            anchor = self._committed[-anchor_len:]
            idx = _rfind_subsequence(norm, anchor)
            if idx != -1:
                return idx + anchor_len
        # No overlap found: either a genuine silence gap between windows or the
        # model diverged on the shared audio.  Emit the whole window so speech
        # is never dropped; an occasional repeat is preferable to lost content.
        logger.warning("transcription overlap not found; emitting full window")
        return 0

    def _commit(self, norm: list[str]) -> None:
        self._committed.extend(norm)
        if len(self._committed) > self._HISTORY_CAP:
            self._committed = self._committed[-self._HISTORY_CAP :]


class WhisperAPIProvider:
    """Posts audio windows to the OpenAI ``/v1/audio/transcriptions`` endpoint.

    Implements the :class:`TranscriptionProvider` protocol.  Works with the
    Whisper (``whisper-1``) and gpt-4o transcription models.

    Incoming WebM/Opus audio is kept in a rolling buffer.  Every *slide_bytes*
    of fresh audio the provider re-transcribes the trailing *window_bytes* of
    audio, giving the model a long, context-rich window.  Overlap between
    consecutive windows is removed before transcript text is yielded.

    Retry with exponential back-off is applied on transient HTTP errors
    (429 / 5xx).  Every successful call records usage via *cost_meter*.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o-mini-transcribe",
        cost_meter: CostMeter | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _MAX_RETRIES,
        backoff_base: float = _BACKOFF_BASE_S,
        window_bytes: int = _DEFAULT_WINDOW_BYTES,
        slide_bytes: int = _DEFAULT_SLIDE_BYTES,
        prompt: str = "",
        http_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.openai.com/v1",
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._cost_meter = cost_meter
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._window_bytes = window_bytes
        self._slide_bytes = slide_bytes
        self._prompt = prompt
        self._base_url = base_url.rstrip("/")
        self._external_client = http_client
        # Optional semaphore that limits the number of concurrent API calls
        # across all provider instances (pass a shared asyncio.Semaphore).
        self._semaphore = semaphore

    # -- TranscriptionProvider protocol ----------------------------------------

    async def transcribe_stream(
        self,
        audio_stream: AsyncIterator[bytes],
        source_language: str | None = None,
    ) -> AsyncIterator[str]:
        """Transcribe audio with a sliding overlap window.

        MediaRecorder emits WebM data where only the *first* chunk contains the
        EBML initialization segment (header + Tracks).  Subsequent chunks are
        bare Cluster continuation data.  We keep the init segment and a deque of
        whole post-init chunks; each transcription window is built as
        ``init + recent clusters`` so every request is a valid WebM file whose
        clusters start on a chunk (cluster) boundary — never sliced mid-cluster.
        """
        webm_init: bytes = b""  # EBML header + Segment Info + Tracks (no audio)
        window: deque[bytes] = deque()  # recent post-init chunks (whole clusters)
        window_bytes = 0  # total bytes currently buffered in *window*
        bytes_since_flush = 0  # fresh audio accumulated since the last API call
        dedup = _OverlapDeduplicator()

        async for chunk in audio_stream:
            if not webm_init:
                # First chunk: split off the initialization segment.
                init_end = _webm_init_end(chunk)
                webm_init = chunk[:init_end]
                payload = chunk[init_end:]
            else:
                payload = chunk

            if payload:
                window.append(payload)
                window_bytes += len(payload)
                bytes_since_flush += len(payload)

            # Trim audio that has slid out of the window (drop whole chunks so
            # the buffer always starts on a cluster boundary).
            while len(window) > 1 and window_bytes > self._window_bytes:
                window_bytes -= len(window.popleft())

            if bytes_since_flush >= self._slide_bytes and window_bytes > 0:
                text = await self._transcribe_window(
                    webm_init, window, source_language
                )
                bytes_since_flush = 0
                new_text = dedup.next_segment(text)
                if new_text:
                    yield new_text

        # Final flush: transcribe any audio not yet sent so the tail of the
        # meeting is never lost.
        if window_bytes > 0 and bytes_since_flush > 0:
            text = await self._transcribe_window(webm_init, window, source_language)
            new_text = dedup.next_segment(text)
            if new_text:
                yield new_text

    # -- Internal helpers ------------------------------------------------------

    async def _transcribe_window(
        self,
        webm_init: bytes,
        window: deque[bytes],
        source_language: str | None,
    ) -> str:
        """POST ``init + window`` audio to the API and return transcript text."""
        audio_bytes = webm_init + b"".join(window)

        data: dict[str, str] = {"model": self._model, "response_format": "text"}
        if source_language:
            data["language"] = source_language
        if self._prompt:
            data["prompt"] = self._prompt

        files = {"file": ("audio.webm", audio_bytes, "audio/webm")}

        logger.info(
            "transcribe: sending %d bytes (~%.1fs) model=%s lang=%s",
            len(audio_bytes),
            len(audio_bytes) / _WEBM_BYTES_PER_SECOND,
            self._model,
            source_language or "auto",
        )
        t0 = time.monotonic()
        try:
            if self._semaphore is not None:
                async with self._semaphore:
                    response = await self._post_with_retry(data=data, files=files)
            else:
                response = await self._post_with_retry(data=data, files=files)
        except Exception:
            provider_errors_total.labels(provider="openai", operation="transcribe").inc()
            raise
        finally:
            segment_transcription_duration_seconds.labels(provider="openai").observe(
                time.monotonic() - t0
            )
        text = response.text.strip()
        logger.info(
            "transcribe: got %d chars in %.2fs lang=%s",
            len(text),
            time.monotonic() - t0,
            source_language or "auto",
        )

        if self._cost_meter is not None:
            # OpenAI bills per audio minute *sent*.  Sliding windows resend
            # overlapping audio, so this is the true (over-1x) billable usage.
            estimated_seconds = max(len(audio_bytes) / _WEBM_BYTES_PER_SECOND, 1.0)
            await self._cost_meter.record(
                provider="openai",
                operation="transcribe_minute",
                units=estimated_seconds / 60.0,
            )

        return text

    async def _post_with_retry(
        self,
        *,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> httpx.Response:
        """POST to the transcriptions endpoint with retry + back-off."""
        url = f"{self._base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        last_exc: Exception | None = None

        client = self._external_client or httpx.AsyncClient()
        owns_client = self._external_client is None

        try:
            for attempt in range(self._max_retries):
                try:
                    resp = await client.post(
                        url,
                        headers=headers,
                        data=data,
                        files=files,
                        timeout=self._timeout,
                    )
                    if resp.status_code < 400:
                        return resp
                    if resp.status_code in (429, 500, 502, 503, 504):
                        logger.warning(
                            "transcription API returned %s on attempt %d/%d",
                            resp.status_code,
                            attempt + 1,
                            self._max_retries,
                        )
                        last_exc = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                    else:
                        # Non-retryable client error (400, 401, 403, etc.)
                        resp.raise_for_status()
                except httpx.TimeoutException as exc:
                    logger.warning(
                        "transcription API timed out on attempt %d/%d",
                        attempt + 1,
                        self._max_retries,
                    )
                    last_exc = exc

                if attempt < self._max_retries - 1:
                    wait = self._backoff_base * (2**attempt)
                    await asyncio.sleep(wait)
        finally:
            if owns_client:
                await client.aclose()

        raise WhisperTranscriptionError(
            f"Transcription API failed after {self._max_retries} attempts"
        ) from last_exc


class WhisperTranscriptionError(Exception):
    """Raised when transcription fails after all retries."""
