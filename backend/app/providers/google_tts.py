"""Google Cloud Text-to-Speech provider."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from app.providers.base import CostMeter

logger = logging.getLogger(__name__)

# Google Cloud TTS pricing: $16 per 1M characters (Standard voices).
_COST_PER_CHAR_USD = 16.0 / 1_000_000

# Defaults -------------------------------------------------------------------
_DEFAULT_TIMEOUT_S = 10.0
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.5

# Language → default voice mapping for the four target languages.
_DEFAULT_VOICES: dict[str, str] = {
    "en": "en-US-Standard-C",
    "es": "es-US-Standard-A",
    "to": "to-Standard-A",
    "tl": "fil-PH-Standard-A",
}


class GoogleTTSError(Exception):
    """Raised when Google Cloud TTS fails after all retries."""


class GoogleTTSProvider:
    """Synthesizes speech via the Google Cloud Text-to-Speech REST API.

    Implements the :class:`TTSProvider` protocol.

    Uses the ``synthesize`` endpoint with an access token obtained from
    the Google metadata server (or supplied directly for testing).

    Retry with exponential back-off is applied on transient HTTP errors
    (429 / 5xx).  Every successful call records character usage via
    *cost_meter*.
    """

    def __init__(
        self,
        *,
        access_token: str | None = None,
        cost_meter: CostMeter | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _MAX_RETRIES,
        backoff_base: float = _BACKOFF_BASE_S,
        http_client: httpx.AsyncClient | None = None,
        voice_overrides: dict[str, str] | None = None,
        audio_encoding: str = "MP3",
    ) -> None:
        self._access_token = access_token
        self._cost_meter = cost_meter
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._external_client = http_client
        self._voices = {**_DEFAULT_VOICES, **(voice_overrides or {})}
        self._audio_encoding = audio_encoding

    # -- TTSProvider protocol --------------------------------------------------

    async def synthesize(self, text: str, language: str) -> bytes:
        """Synthesize *text* in *language*, returning raw audio bytes (MP3)."""
        voice_name = self._voices.get(language)
        # Derive the language code the API expects from the voice name.
        if voice_name:
            language_code = "-".join(voice_name.split("-")[:2])
        else:
            language_code = language
            voice_name = None  # Let the API pick a default.

        url = "https://texttospeech.googleapis.com/v1/text:synthesize"

        payload: dict = {
            "input": {"text": text},
            "voice": {"languageCode": language_code},
            "audioConfig": {"audioEncoding": self._audio_encoding},
        }
        if voice_name:
            payload["voice"]["name"] = voice_name

        headers = await self._auth_headers()
        headers["Content-Type"] = "application/json"

        response = await self._post_with_retry(url, json=payload, headers=headers)
        data = response.json()

        audio_content = data.get("audioContent")
        if not audio_content:
            raise GoogleTTSError("Empty audioContent in TTS response")

        audio_bytes = base64.b64decode(audio_content)

        if self._cost_meter is not None:
            await self._cost_meter.record(
                provider="google",
                operation="tts_char",
                units=float(len(text)),
            )

        return audio_bytes

    # -- Internal helpers ------------------------------------------------------

    async def _auth_headers(self) -> dict[str, str]:
        """Build authorization headers.

        Same pattern as GoogleV3TranslationProvider: explicit token first,
        then fall back to ADC / metadata server.
        """
        if self._access_token:
            return {"Authorization": f"Bearer {self._access_token}"}

        token = await self._fetch_adc_token()
        return {"Authorization": f"Bearer {token}"}

    async def _fetch_adc_token(self) -> str:
        """Obtain an access token via Application Default Credentials."""
        import json
        import subprocess  # noqa: S404

        try:
            result = subprocess.run(  # noqa: S603, S607
                ["gcloud", "auth", "application-default", "print-access-token"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Fallback: GCE metadata server.
        client = self._external_client or httpx.AsyncClient()
        owns_client = self._external_client is None
        try:
            resp = await client.get(
                "http://metadata.google.internal/computeMetadata/v1/"
                "instance/service-accounts/default/token",
                headers={"Metadata-Flavor": "Google"},
                timeout=5.0,
            )
            resp.raise_for_status()
            return json.loads(resp.text)["access_token"]
        finally:
            if owns_client:
                await client.aclose()

    async def _post_with_retry(
        self,
        url: str,
        *,
        json: dict,
        headers: dict[str, str],
    ) -> httpx.Response:
        """POST with retry + exponential back-off on transient errors."""
        last_exc: Exception | None = None

        client = self._external_client or httpx.AsyncClient()
        owns_client = self._external_client is None

        try:
            for attempt in range(self._max_retries):
                try:
                    resp = await client.post(
                        url,
                        json=json,
                        headers=headers,
                        timeout=self._timeout,
                    )
                    if resp.status_code < 400:
                        return resp
                    if resp.status_code in (429, 500, 502, 503, 504):
                        logger.warning(
                            "Google TTS API returned %s on attempt %d/%d",
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
                        resp.raise_for_status()
                except httpx.TimeoutException as exc:
                    logger.warning(
                        "Google TTS API timed out on attempt %d/%d",
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

        raise GoogleTTSError(
            f"Google TTS API failed after {self._max_retries} attempts"
        ) from last_exc
