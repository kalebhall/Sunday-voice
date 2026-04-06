"""OpenAI Whisper API transcription provider."""

from __future__ import annotations

import io
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from app.providers.base import CostMeter

logger = logging.getLogger(__name__)

# Whisper API pricing: $0.006 per minute of audio.
_WHISPER_COST_PER_MINUTE = 0.006

# Defaults -------------------------------------------------------------------
_DEFAULT_TIMEOUT_S = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 1.0  # 1s, 2s, 4s
_CHUNK_FLUSH_BYTES = 1 * 1024 * 1024  # flush to API every ~1 MiB of audio


class WhisperAPIProvider:
    """Posts audio chunks to the OpenAI Whisper ``/v1/audio/transcriptions`` endpoint.

    Implements the :class:`TranscriptionProvider` protocol.

    Audio bytes arriving from the async iterator are buffered internally.
    Each time the buffer exceeds *chunk_flush_bytes* (default 1 MiB) — or when
    the stream ends — the buffered audio is POSTed to Whisper as a single file
    upload.  The returned transcript text is yielded back to the caller.

    Retry with exponential back-off is applied on transient HTTP errors
    (429 / 5xx).  Every successful call records usage via *cost_meter*.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "whisper-1",
        cost_meter: CostMeter | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _MAX_RETRIES,
        backoff_base: float = _BACKOFF_BASE_S,
        chunk_flush_bytes: int = _CHUNK_FLUSH_BYTES,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._cost_meter = cost_meter
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._chunk_flush_bytes = chunk_flush_bytes
        self._base_url = base_url.rstrip("/")
        self._external_client = http_client

    # -- TranscriptionProvider protocol ----------------------------------------

    async def transcribe_stream(
        self,
        audio_stream: AsyncIterator[bytes],
        source_language: str | None = None,
    ) -> AsyncIterator[str]:
        """Buffer audio chunks and yield transcript segments."""
        buf = io.BytesIO()

        async for chunk in audio_stream:
            buf.write(chunk)
            if buf.tell() >= self._chunk_flush_bytes:
                text = await self._transcribe_buffer(buf, source_language)
                if text:
                    yield text
                buf = io.BytesIO()

        # Flush remaining bytes.
        if buf.tell() > 0:
            text = await self._transcribe_buffer(buf, source_language)
            if text:
                yield text

    # -- Internal helpers ------------------------------------------------------

    async def _transcribe_buffer(
        self,
        buf: io.BytesIO,
        source_language: str | None,
    ) -> str:
        """POST buffered audio to Whisper and return the transcript text."""
        buf.seek(0)
        audio_bytes = buf.read()

        data: dict[str, str] = {"model": self._model}
        if source_language:
            data["language"] = source_language
        data["response_format"] = "text"

        files = {"file": ("audio.webm", audio_bytes, "audio/webm")}

        response = await self._post_with_retry(data=data, files=files)
        text = response.text.strip()

        if self._cost_meter is not None:
            # Estimate audio duration from raw byte size.  WebM/Opus ≈ 16 kB/s
            # at typical speech bit-rates, so bytes / 16000 ≈ seconds.
            estimated_seconds = max(len(audio_bytes) / 16_000, 1.0)
            estimated_minutes = estimated_seconds / 60.0
            await self._cost_meter.record(
                provider="openai",
                operation="transcribe_minute",
                units=estimated_minutes,
            )

        return text

    async def _post_with_retry(
        self,
        *,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> httpx.Response:
        """POST to the transcriptions endpoint with retry + back-off."""
        import asyncio

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
                            "Whisper API returned %s on attempt %d/%d",
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
                        "Whisper API timed out on attempt %d/%d",
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
            f"Whisper API failed after {self._max_retries} attempts"
        ) from last_exc


class WhisperTranscriptionError(Exception):
    """Raised when Whisper transcription fails after all retries."""
