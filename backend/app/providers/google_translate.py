"""Google Cloud Translation API v2 (Basic) provider."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from app.providers.base import CostMeter

logger = logging.getLogger(__name__)

# Google Cloud Translation v2 pricing: $20 per 1M characters.
# First 500,000 characters per month are free.
_COST_PER_CHAR_USD = 20.0 / 1_000_000

_DEFAULT_TIMEOUT_S = 10.0
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.5  # 0.5s, 1s, 2s

_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"


class GoogleTranslationError(Exception):
    """Raised when Google Cloud Translation fails after all retries."""


class GoogleTranslationProvider:
    """Translates text via the Google Cloud Translation v2 REST API.

    Implements the :class:`TranslationProvider` protocol.

    Authentication uses a plain API key passed as a query parameter — no
    service account or OAuth setup required.

    Retry with exponential back-off is applied on transient HTTP errors
    (429 / 5xx).  Every successful call records character usage via
    *cost_meter*.
    """

    def __init__(
        self,
        *,
        api_key: str,
        cost_meter: CostMeter | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _MAX_RETRIES,
        backoff_base: float = _BACKOFF_BASE_S,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
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

        payload = {
            "q": text,
            "source": source_language,
            "target": target_language,
            "format": "text",
        }

        response = await self._post_with_retry(
            _TRANSLATE_URL,
            params={"key": self._api_key},
            json=payload,
        )
        data = response.json()

        translations = data.get("data", {}).get("translations", [])
        if not translations:
            raise GoogleTranslationError(
                f"Empty translations response for {source_language}->{target_language}"
            )

        translated_text: str = translations[0]["translatedText"]

        if self._cost_meter is not None:
            await self._cost_meter.record(
                provider="google",
                operation="translate_char",
                units=float(len(text)),
            )

        return translated_text

    # -- Internal helpers ------------------------------------------------------

    async def _post_with_retry(
        self,
        url: str,
        *,
        params: dict,
        json: dict,
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
                        params=params,
                        json=json,
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
