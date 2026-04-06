"""Unit tests for WhisperAPIProvider with mocked HTTP client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest

from app.providers.whisper import WhisperAPIProvider, WhisperTranscriptionError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Simulated audio fixture: 800 bytes of silence (below the 1 MiB flush
# threshold so a single stream yields one transcript at stream-end).
AUDIO_FIXTURE_SMALL = b"\x00" * 800

# Large enough to trigger a mid-stream flush (> 1 MiB).
AUDIO_FIXTURE_LARGE = b"\x00" * (1024 * 1024 + 512)


async def _audio_chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for p in parts:
        yield p


@dataclass
class RecordedUsage:
    provider: str
    operation: str
    units: float


class FakeCostMeter:
    """In-memory CostMeter that records calls for assertions."""

    records: list[RecordedUsage] = field(default_factory=list)

    def __init__(self) -> None:
        self.records: list[RecordedUsage] = []

    async def record(self, provider: str, operation: str, units: float) -> None:
        self.records.append(RecordedUsage(provider, operation, units))


# ---------------------------------------------------------------------------
# Mock HTTP transport
# ---------------------------------------------------------------------------


class _MockTransport(httpx.AsyncBaseTransport):
    """Returns canned responses and records requests for inspection."""

    def __init__(
        self,
        responses: list[httpx.Response] | None = None,
        *,
        status: int = 200,
        text: str = "Hello world",
    ) -> None:
        # If explicit response list given, serve them in order.
        if responses is not None:
            self._responses = list(responses)
        else:
            self._responses = [
                httpx.Response(status, text=text),
            ]
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Read body so it's available for inspection.
        await request.aread()
        self.requests.append(request)
        if self._responses:
            resp = self._responses.pop(0)
        else:
            # If we run out, keep returning the last one.
            resp = httpx.Response(200, text="fallback")
        resp.request = request
        # Stream must be set for httpx internals.
        resp.stream = httpx.ByteStream(resp.text.encode() if resp.text else b"")
        return resp


def _make_client(transport: _MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="https://api.openai.com")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTranscribeStream:
    """Basic transcription flow."""

    @pytest.mark.asyncio
    async def test_single_chunk_returns_transcript(self) -> None:
        transport = _MockTransport(text="Good morning everyone.")
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-test",
            http_client=client,
        )

        segments: list[str] = []
        async for seg in provider.transcribe_stream(
            _audio_chunks(AUDIO_FIXTURE_SMALL),
        ):
            segments.append(seg)

        assert segments == ["Good morning everyone."]
        assert len(transport.requests) == 1

    @pytest.mark.asyncio
    async def test_source_language_forwarded(self) -> None:
        transport = _MockTransport(text="Buenos días.")
        client = _make_client(transport)
        provider = WhisperAPIProvider(api_key="sk-test", http_client=client)

        segments: list[str] = []
        async for seg in provider.transcribe_stream(
            _audio_chunks(AUDIO_FIXTURE_SMALL),
            source_language="es",
        ):
            segments.append(seg)

        assert segments == ["Buenos días."]
        # Verify the language param was sent in the multipart form.
        req = transport.requests[0]
        body = req.content.decode("utf-8", errors="replace")
        assert "es" in body

    @pytest.mark.asyncio
    async def test_large_audio_flushes_mid_stream(self) -> None:
        """Audio exceeding chunk_flush_bytes triggers an early API call."""
        transport = _MockTransport(
            responses=[
                httpx.Response(200, text="Part one."),
                httpx.Response(200, text="Part two."),
            ]
        )
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-test",
            http_client=client,
            chunk_flush_bytes=1024 * 1024,  # 1 MiB
        )

        # Send a big chunk followed by a small one.
        segments: list[str] = []
        async for seg in provider.transcribe_stream(
            _audio_chunks(AUDIO_FIXTURE_LARGE, AUDIO_FIXTURE_SMALL),
        ):
            segments.append(seg)

        assert segments == ["Part one.", "Part two."]
        assert len(transport.requests) == 2

    @pytest.mark.asyncio
    async def test_empty_transcript_not_yielded(self) -> None:
        transport = _MockTransport(text="   ")
        client = _make_client(transport)
        provider = WhisperAPIProvider(api_key="sk-test", http_client=client)

        segments: list[str] = []
        async for seg in provider.transcribe_stream(
            _audio_chunks(AUDIO_FIXTURE_SMALL),
        ):
            segments.append(seg)

        assert segments == []


class TestCostMetering:
    """CostMeter is invoked with correct provider/operation."""

    @pytest.mark.asyncio
    async def test_cost_recorded_on_success(self) -> None:
        meter = FakeCostMeter()
        transport = _MockTransport(text="Talofa.")
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-test",
            http_client=client,
            cost_meter=meter,
        )

        async for _ in provider.transcribe_stream(
            _audio_chunks(AUDIO_FIXTURE_SMALL),
        ):
            pass

        assert len(meter.records) == 1
        rec = meter.records[0]
        assert rec.provider == "openai"
        assert rec.operation == "transcribe_minute"
        assert rec.units > 0

    @pytest.mark.asyncio
    async def test_no_cost_without_meter(self) -> None:
        """Provider works fine without a CostMeter."""
        transport = _MockTransport(text="OK")
        client = _make_client(transport)
        provider = WhisperAPIProvider(api_key="sk-test", http_client=client)

        segments: list[str] = []
        async for seg in provider.transcribe_stream(
            _audio_chunks(AUDIO_FIXTURE_SMALL),
        ):
            segments.append(seg)

        assert segments == ["OK"]


class TestRetryWithBackoff:
    """Retry logic on transient errors."""

    @pytest.mark.asyncio
    async def test_retries_on_429(self) -> None:
        transport = _MockTransport(
            responses=[
                httpx.Response(429, text="rate limited"),
                httpx.Response(200, text="Recovered."),
            ]
        )
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-test",
            http_client=client,
            max_retries=3,
            backoff_base=0.01,  # fast for tests
        )

        segments: list[str] = []
        async for seg in provider.transcribe_stream(
            _audio_chunks(AUDIO_FIXTURE_SMALL),
        ):
            segments.append(seg)

        assert segments == ["Recovered."]
        assert len(transport.requests) == 2

    @pytest.mark.asyncio
    async def test_retries_on_500(self) -> None:
        transport = _MockTransport(
            responses=[
                httpx.Response(500, text="Internal Server Error"),
                httpx.Response(503, text="Service Unavailable"),
                httpx.Response(200, text="Finally."),
            ]
        )
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-test",
            http_client=client,
            max_retries=3,
            backoff_base=0.01,
        )

        segments: list[str] = []
        async for seg in provider.transcribe_stream(
            _audio_chunks(AUDIO_FIXTURE_SMALL),
        ):
            segments.append(seg)

        assert segments == ["Finally."]
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
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-test",
            http_client=client,
            max_retries=3,
            backoff_base=0.01,
        )

        with pytest.raises(WhisperTranscriptionError, match="failed after 3 attempts"):
            async for _ in provider.transcribe_stream(
                _audio_chunks(AUDIO_FIXTURE_SMALL),
            ):
                pass

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self) -> None:
        """4xx errors (other than 429) are not retried."""
        transport = _MockTransport(
            responses=[
                httpx.Response(401, text="Unauthorized"),
            ]
        )
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-bad",
            http_client=client,
            max_retries=3,
            backoff_base=0.01,
        )

        with pytest.raises(httpx.HTTPStatusError):
            async for _ in provider.transcribe_stream(
                _audio_chunks(AUDIO_FIXTURE_SMALL),
            ):
                pass

        # Only one request — no retries.
        assert len(transport.requests) == 1


class TestTimeout:
    """Timeout handling."""

    @pytest.mark.asyncio
    async def test_timeout_triggers_retry(self) -> None:
        call_count = 0

        class _TimeoutThenOk(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                await request.aread()
                if call_count == 1:
                    raise httpx.ReadTimeout("timed out")
                resp = httpx.Response(200, text="After timeout.")
                resp.request = request
                resp.stream = httpx.ByteStream(b"After timeout.")
                return resp

        client = httpx.AsyncClient(
            transport=_TimeoutThenOk(), base_url="https://api.openai.com"
        )
        provider = WhisperAPIProvider(
            api_key="sk-test",
            http_client=client,
            max_retries=3,
            backoff_base=0.01,
        )

        segments: list[str] = []
        async for seg in provider.transcribe_stream(
            _audio_chunks(AUDIO_FIXTURE_SMALL),
        ):
            segments.append(seg)

        assert segments == ["After timeout."]
        assert call_count == 2


class TestProtocolConformance:
    """WhisperAPIProvider satisfies the TranscriptionProvider protocol."""

    def test_isinstance_check(self) -> None:
        from app.providers.base import TranscriptionProvider

        provider = WhisperAPIProvider(api_key="sk-test")
        assert isinstance(provider, TranscriptionProvider)
