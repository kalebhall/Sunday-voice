"""Unit tests for GoogleTranslationProvider with mocked HTTP client."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest

from app.providers.google_translate import (
    GoogleTranslationError,
    GoogleTranslationProvider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class RecordedUsage:
    provider: str
    operation: str
    units: float


class FakeCostMeter:
    """In-memory CostMeter that records calls for assertions."""

    def __init__(self) -> None:
        self.records: list[RecordedUsage] = []

    async def record(self, provider: str, operation: str, units: float) -> None:
        self.records.append(RecordedUsage(provider, operation, units))


# ---------------------------------------------------------------------------
# Mock HTTP transport
# ---------------------------------------------------------------------------


def _translate_response(text: str, status: int = 200) -> httpx.Response:
    body = json.dumps({"data": {"translations": [{"translatedText": text}]}})
    return httpx.Response(status, text=body)


class _MockTransport(httpx.AsyncBaseTransport):
    """Returns canned responses and records requests for inspection."""

    def __init__(
        self,
        responses: list[httpx.Response] | None = None,
        *,
        translated_text: str = "Hola mundo",
    ) -> None:
        if responses is not None:
            self._responses = list(responses)
        else:
            self._responses = [_translate_response(translated_text)]
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        if self._responses:
            resp = self._responses.pop(0)
        else:
            resp = _translate_response("fallback")
        resp.request = request
        resp.stream = httpx.ByteStream(resp.text.encode() if resp.text else b"")
        return resp


def _make_provider(
    transport: _MockTransport,
    *,
    cost_meter: FakeCostMeter | None = None,
    max_retries: int = 3,
    backoff_base: float = 0.01,
) -> GoogleTranslationProvider:
    client = httpx.AsyncClient(
        transport=transport, base_url="https://translation.googleapis.com"
    )
    return GoogleTranslationProvider(
        api_key="fake-api-key",
        cost_meter=cost_meter,
        max_retries=max_retries,
        backoff_base=backoff_base,
        http_client=client,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTranslate:
    """Basic translation flow."""

    @pytest.mark.asyncio
    async def test_translates_text(self) -> None:
        transport = _MockTransport(translated_text="Buenos días")
        provider = _make_provider(transport)

        result = await provider.translate("Good morning", "en", "es")

        assert result == "Buenos días"
        assert len(transport.requests) == 1

    @pytest.mark.asyncio
    async def test_same_language_returns_original(self) -> None:
        transport = _MockTransport()
        provider = _make_provider(transport)

        result = await provider.translate("Hello", "en", "en")

        assert result == "Hello"
        assert len(transport.requests) == 0  # No API call made

    @pytest.mark.asyncio
    async def test_request_payload_structure(self) -> None:
        transport = _MockTransport(translated_text="Talofa")
        provider = _make_provider(transport)

        await provider.translate("Hello", "en", "to")

        req = transport.requests[0]
        body = json.loads(req.content.decode())
        assert body["q"] == "Hello"
        assert body["source"] == "en"
        assert body["target"] == "to"
        assert body["format"] == "text"

    @pytest.mark.asyncio
    async def test_api_key_in_url(self) -> None:
        transport = _MockTransport(translated_text="Kumusta")
        provider = _make_provider(transport)

        await provider.translate("Hello", "en", "tl")

        req = transport.requests[0]
        assert "key=fake-api-key" in str(req.url)

    @pytest.mark.asyncio
    async def test_url_is_v2_endpoint(self) -> None:
        transport = _MockTransport(translated_text="Hola")
        provider = _make_provider(transport)

        await provider.translate("Hello", "en", "es")

        req = transport.requests[0]
        assert "/language/translate/v2" in str(req.url)

    @pytest.mark.asyncio
    async def test_empty_translations_raises(self) -> None:
        empty_resp = httpx.Response(
            200, text=json.dumps({"data": {"translations": []}})
        )
        transport = _MockTransport(responses=[empty_resp])
        provider = _make_provider(transport)

        with pytest.raises(GoogleTranslationError, match="Empty translations"):
            await provider.translate("Hello", "en", "es")


class TestCostMetering:
    """CostMeter is invoked with correct provider/operation/units."""

    @pytest.mark.asyncio
    async def test_cost_recorded_on_success(self) -> None:
        meter = FakeCostMeter()
        transport = _MockTransport(translated_text="Hola")
        provider = _make_provider(transport, cost_meter=meter)

        await provider.translate("Hello world", "en", "es")

        assert len(meter.records) == 1
        rec = meter.records[0]
        assert rec.provider == "google"
        assert rec.operation == "translate_char"
        assert rec.units == len("Hello world")

    @pytest.mark.asyncio
    async def test_no_cost_without_meter(self) -> None:
        transport = _MockTransport(translated_text="Hola")
        provider = _make_provider(transport)

        result = await provider.translate("Hello", "en", "es")
        assert result == "Hola"

    @pytest.mark.asyncio
    async def test_same_language_no_cost(self) -> None:
        meter = FakeCostMeter()
        transport = _MockTransport()
        provider = _make_provider(transport, cost_meter=meter)

        await provider.translate("Hello", "en", "en")

        assert len(meter.records) == 0


class TestRetryWithBackoff:
    """Retry logic on transient errors."""

    @pytest.mark.asyncio
    async def test_retries_on_429(self) -> None:
        transport = _MockTransport(
            responses=[
                httpx.Response(429, text="rate limited"),
                _translate_response("Recovered"),
            ]
        )
        provider = _make_provider(transport)

        result = await provider.translate("Hello", "en", "es")
        assert result == "Recovered"
        assert len(transport.requests) == 2

    @pytest.mark.asyncio
    async def test_retries_on_500(self) -> None:
        transport = _MockTransport(
            responses=[
                httpx.Response(500, text="Internal Server Error"),
                httpx.Response(503, text="Service Unavailable"),
                _translate_response("Finally"),
            ]
        )
        provider = _make_provider(transport)

        result = await provider.translate("Hello", "en", "es")
        assert result == "Finally"
        assert len(transport.requests) == 3

    @pytest.mark.asyncio
    async def test_exhausted_retries_raises(self) -> None:
        transport = _MockTransport(
            responses=[
                httpx.Response(500, text="fail"),
                httpx.Response(500, text="fail"),
                httpx.Response(500, text="fail"),
            ]
        )
        provider = _make_provider(transport)

        with pytest.raises(GoogleTranslationError, match="failed after 3 attempts"):
            await provider.translate("Hello", "en", "es")

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self) -> None:
        transport = _MockTransport(
            responses=[httpx.Response(403, text="Forbidden")]
        )
        provider = _make_provider(transport)

        with pytest.raises(httpx.HTTPStatusError):
            await provider.translate("Hello", "en", "es")

        assert len(transport.requests) == 1


class TestProtocolConformance:
    """GoogleTranslationProvider satisfies the TranslationProvider protocol."""

    def test_isinstance_check(self) -> None:
        from app.providers.base import TranslationProvider

        provider = GoogleTranslationProvider(api_key="test-key")
        assert isinstance(provider, TranslationProvider)
