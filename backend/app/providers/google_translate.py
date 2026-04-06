"""Google Cloud Translation API v3 provider."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from app.providers.base import CostMeter

logger = logging.getLogger(__name__)

# Google Cloud Translation v3 pricing: $20 per 1M characters.
_COST_PER_CHAR_USD = 20.0 / 1_000_000

# Defaults -------------------------------------------------------------------
_DEFAULT_TIMEOUT_S = 10.0
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.5  # 0.5s, 1s, 2s


class GoogleTranslationError(Exception):
    """Raised when Google Cloud Translation fails after all retries."""


class GoogleV3TranslationProvider:
    """Translates text via the Google Cloud Translation v3 REST API.

    Implements the :class:`TranslationProvider` protocol.

    Uses the ``translateText`` endpoint with an access token obtained from
    the Google metadata server (or supplied directly for testing).

    Retry with exponential back-off is applied on transient HTTP errors
    (429 / 5xx).  Every successful call records character usage via
    *cost_meter*.
    """

    def __init__(
        self,
        *,
        project: str,
        location: str = "global",
        access_token: str | None = None,
        credentials_file: str | None = None,
        cost_meter: CostMeter | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _MAX_RETRIES,
        backoff_base: float = _BACKOFF_BASE_S,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._project = project
        self._location = location
        self._access_token = access_token
        self._credentials_file = credentials_file
        self._cost_meter = cost_meter
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._external_client = http_client

    # -- TranslationProvider protocol ------------------------------------------

    async def translate(
        self, text: str, source_language: str, target_language: str
    ) -> str:
        """Translate *text* from *source_language* to *target_language*."""
        if source_language == target_language:
            return text

        parent = f"projects/{self._project}/locations/{self._location}"
        url = (
            f"https://translation.googleapis.com/v3/{parent}:translateText"
        )

        payload = {
            "contents": [text],
            "sourceLanguageCode": source_language,
            "targetLanguageCode": target_language,
            "mimeType": "text/plain",
        }

        headers = await self._auth_headers()
        headers["Content-Type"] = "application/json"

        response = await self._post_with_retry(url, json=payload, headers=headers)
        data = response.json()

        translations = data.get("translations", [])
        if not translations:
            raise GoogleTranslationError(
                f"Empty translations response for {source_language}->{target_language}"
            )

        translated_text: str = translations[0]["translatedText"]

        if self._cost_meter is not None:
            char_count = len(text)
            await self._cost_meter.record(
                provider="google",
                operation="translate_char",
                units=float(char_count),
            )

        return translated_text

    # -- Internal helpers ------------------------------------------------------

    async def _auth_headers(self) -> dict[str, str]:
        """Build authorization headers.

        If an explicit *access_token* was provided (e.g. in tests), use it.
        Otherwise fall back to the GCE metadata server which works inside
        Google Cloud environments and when ``GOOGLE_APPLICATION_CREDENTIALS``
        is set and the ``gcloud`` CLI is authenticated.
        """
        if self._access_token:
            return {"Authorization": f"Bearer {self._access_token}"}

        # Fetch token from metadata server / ADC.
        token = await self._fetch_adc_token()
        return {"Authorization": f"Bearer {token}"}

    async def _fetch_adc_token(self) -> str:
        """Obtain an access token via Application Default Credentials.

        Uses the ``gcloud`` helper endpoint when GOOGLE_APPLICATION_CREDENTIALS
        is set, or the GCE metadata server when running on Google Cloud.
        """
        # Use google-auth library via a subprocess-free approach:
        # Shell out to the well-known token endpoint.
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
                            "Google Translate API returned %s on attempt %d/%d",
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
                        "Google Translate API timed out on attempt %d/%d",
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

        raise GoogleTranslationError(
            f"Google Translate API failed after {self._max_retries} attempts"
        ) from last_exc
