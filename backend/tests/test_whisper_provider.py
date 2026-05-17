"""Unit tests for WhisperAPIProvider with mocked HTTP client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest

from app.providers.whisper import (
    WhisperAPIProvider,
    WhisperTranscriptionError,
    _OverlapDeduplicator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A WebM Cluster element ID marks the end of the initialization segment.
_CLUSTER_ID = b"\x1f\x43\xb6\x75"

# Fake WebM init segment (EBML header + Tracks): any bytes without a Cluster ID.
_WEBM_INIT = b"\x1a\x45\xdf\xa3" + b"\x11" * 36  # 40 bytes


def _first_chunk(audio_bytes: int) -> bytes:
    """A realistic first MediaRecorder chunk: init segment + one Cluster."""
    return _WEBM_INIT + _CLUSTER_ID + b"\x00" * audio_bytes


def _cont_chunk(size: int) -> bytes:
    """A continuation chunk (bare Cluster data, no init segment)."""
    return b"\x00" * size


# Single small chunk — below the slide threshold, so only the final flush fires.
AUDIO_FIXTURE_SMALL = _first_chunk(760)


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
        if responses is not None:
            self._responses = list(responses)
        else:
            self._responses = [httpx.Response(status, text=text)]
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        if self._responses:
            resp = self._responses.pop(0)
        else:
            resp = httpx.Response(200, text="fallback")
        resp.request = request
        resp.stream = httpx.ByteStream(resp.text.encode() if resp.text else b"")
        return resp


def _make_client(transport: _MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="https://api.openai.com")


# ---------------------------------------------------------------------------
# Overlap de-duplicator
# ---------------------------------------------------------------------------


class TestOverlapDeduplicator:
    """The de-duplicator strips text already emitted by earlier windows."""

    def test_first_segment_emitted_whole(self) -> None:
        dedup = _OverlapDeduplicator()
        assert dedup.next_segment("Good morning everyone") == "Good morning everyone"

    def test_overlapping_segment_trimmed(self) -> None:
        dedup = _OverlapDeduplicator()
        dedup.next_segment("Good morning brothers and sisters")
        # Next window re-transcribes the tail and adds new words.
        out = dedup.next_segment("morning brothers and sisters welcome to sacrament meeting")
        assert out == "welcome to sacrament meeting"

    def test_no_new_speech_yields_empty(self) -> None:
        dedup = _OverlapDeduplicator()
        dedup.next_segment("please open your hymnbooks")
        assert dedup.next_segment("please open your hymnbooks") == ""

    def test_punctuation_and_case_ignored_for_matching(self) -> None:
        dedup = _OverlapDeduplicator()
        dedup.next_segment("we will now sing hymn number two")
        out = dedup.next_segment("Hymn, number two. Then the opening prayer.")
        assert out == "Then the opening prayer."

    def test_no_overlap_emits_full_window(self) -> None:
        """A genuine gap (silence) between windows is emitted in full."""
        dedup = _OverlapDeduplicator()
        dedup.next_segment("the first speaker has finished")
        out = dedup.next_segment("completely different sentence with no shared words")
        assert out == "completely different sentence with no shared words"


# ---------------------------------------------------------------------------
# Transcription stream
# ---------------------------------------------------------------------------


class TestTranscribeStream:
    """Basic transcription flow."""

    @pytest.mark.asyncio
    async def test_single_chunk_returns_transcript(self) -> None:
        transport = _MockTransport(text="Good morning everyone.")
        client = _make_client(transport)
        provider = WhisperAPIProvider(api_key="sk-test", http_client=client)

        segments: list[str] = []
        async for seg in provider.transcribe_stream(_audio_chunks(AUDIO_FIXTURE_SMALL)):
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
            _audio_chunks(AUDIO_FIXTURE_SMALL), source_language="es"
        ):
            segments.append(seg)

        assert segments == ["Buenos días."]
        body = transport.requests[0].content.decode("utf-8", errors="replace")
        assert "es" in body

    @pytest.mark.asyncio
    async def test_prompt_forwarded(self) -> None:
        transport = _MockTransport(text="The bishop spoke.")
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-test", http_client=client, prompt="ward stake sacrament"
        )

        async for _ in provider.transcribe_stream(_audio_chunks(AUDIO_FIXTURE_SMALL)):
            pass

        body = transport.requests[0].content.decode("utf-8", errors="replace")
        assert "ward stake sacrament" in body

    @pytest.mark.asyncio
    async def test_model_forwarded(self) -> None:
        transport = _MockTransport(text="Hello.")
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-test", http_client=client, model="gpt-4o-mini-transcribe"
        )

        async for _ in provider.transcribe_stream(_audio_chunks(AUDIO_FIXTURE_SMALL)):
            pass

        body = transport.requests[0].content.decode("utf-8", errors="replace")
        assert "gpt-4o-mini-transcribe" in body

    @pytest.mark.asyncio
    async def test_sliding_window_flushes_repeatedly(self) -> None:
        """Fresh audio exceeding slide_bytes triggers a window transcription."""
        transport = _MockTransport(
            responses=[
                httpx.Response(200, text="one"),
                httpx.Response(200, text="two"),
                httpx.Response(200, text="three"),
            ]
        )
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-test",
            http_client=client,
            window_bytes=1000,
            slide_bytes=100,
        )

        # First chunk carries the init segment + 100 bytes of cluster payload;
        # two more 100-byte continuation chunks each trigger another flush.
        async for _ in provider.transcribe_stream(
            _audio_chunks(
                _first_chunk(96),  # payload = 4-byte ClusterID + 96 = 100 bytes
                _cont_chunk(100),
                _cont_chunk(100),
            )
        ):
            pass

        assert len(transport.requests) == 3

    @pytest.mark.asyncio
    async def test_sliding_window_deduplicates_overlap(self) -> None:
        """Overlapping window transcripts yield only newly spoken text."""
        transport = _MockTransport(
            responses=[
                httpx.Response(200, text="Good morning brothers and sisters"),
                httpx.Response(200, text="brothers and sisters welcome to sacrament meeting"),
                httpx.Response(200, text="welcome to sacrament meeting please open the hymnbook"),
            ]
        )
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-test",
            http_client=client,
            window_bytes=1000,
            slide_bytes=100,
        )

        segments: list[str] = []
        async for seg in provider.transcribe_stream(
            _audio_chunks(_first_chunk(96), _cont_chunk(100), _cont_chunk(100))
        ):
            segments.append(seg)

        assert segments == [
            "Good morning brothers and sisters",
            "welcome to sacrament meeting",
            "please open the hymnbook",
        ]

    @pytest.mark.asyncio
    async def test_window_trims_old_audio(self) -> None:
        """Audio beyond window_bytes is dropped so requests stay bounded."""
        transport = _MockTransport(text="text")
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-test",
            http_client=client,
            window_bytes=250,
            slide_bytes=100,
        )

        async for _ in provider.transcribe_stream(
            _audio_chunks(
                _first_chunk(96),
                _cont_chunk(100),
                _cont_chunk(100),
                _cont_chunk(100),
            )
        ):
            pass

        # Four flushes fire.  Without trimming each request would carry more
        # audio than the last; with a 250-byte window the request size grows
        # once and then plateaus instead of growing unboundedly.
        sizes = [len(req.content) for req in transport.requests]
        assert len(sizes) == 4
        assert sizes[1] > sizes[0]  # window filling up
        assert sizes[1] == sizes[2] == sizes[3]  # trimmed to a steady size

    @pytest.mark.asyncio
    async def test_empty_transcript_not_yielded(self) -> None:
        transport = _MockTransport(text="   ")
        client = _make_client(transport)
        provider = WhisperAPIProvider(api_key="sk-test", http_client=client)

        segments: list[str] = []
        async for seg in provider.transcribe_stream(_audio_chunks(AUDIO_FIXTURE_SMALL)):
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
            api_key="sk-test", http_client=client, cost_meter=meter
        )

        async for _ in provider.transcribe_stream(_audio_chunks(AUDIO_FIXTURE_SMALL)):
            pass

        assert len(meter.records) == 1
        rec = meter.records[0]
        assert rec.provider == "openai"
        assert rec.operation == "transcribe_minute"
        assert rec.units > 0

    @pytest.mark.asyncio
    async def test_no_cost_without_meter(self) -> None:
        transport = _MockTransport(text="OK")
        client = _make_client(transport)
        provider = WhisperAPIProvider(api_key="sk-test", http_client=client)

        segments: list[str] = []
        async for seg in provider.transcribe_stream(_audio_chunks(AUDIO_FIXTURE_SMALL)):
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
            api_key="sk-test", http_client=client, max_retries=3, backoff_base=0.01
        )

        segments: list[str] = []
        async for seg in provider.transcribe_stream(_audio_chunks(AUDIO_FIXTURE_SMALL)):
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
            api_key="sk-test", http_client=client, max_retries=3, backoff_base=0.01
        )

        segments: list[str] = []
        async for seg in provider.transcribe_stream(_audio_chunks(AUDIO_FIXTURE_SMALL)):
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
            api_key="sk-test", http_client=client, max_retries=3, backoff_base=0.01
        )

        with pytest.raises(WhisperTranscriptionError, match="failed after 3 attempts"):
            async for _ in provider.transcribe_stream(_audio_chunks(AUDIO_FIXTURE_SMALL)):
                pass

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self) -> None:
        """4xx errors (other than 429) are not retried."""
        transport = _MockTransport(responses=[httpx.Response(401, text="Unauthorized")])
        client = _make_client(transport)
        provider = WhisperAPIProvider(
            api_key="sk-bad", http_client=client, max_retries=3, backoff_base=0.01
        )

        with pytest.raises(httpx.HTTPStatusError):
            async for _ in provider.transcribe_stream(_audio_chunks(AUDIO_FIXTURE_SMALL)):
                pass

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
            api_key="sk-test", http_client=client, max_retries=3, backoff_base=0.01
        )

        segments: list[str] = []
        async for seg in provider.transcribe_stream(_audio_chunks(AUDIO_FIXTURE_SMALL)):
            segments.append(seg)

        assert segments == ["After timeout."]
        assert call_count == 2


class TestProtocolConformance:
    """WhisperAPIProvider satisfies the TranscriptionProvider protocol."""

    def test_isinstance_check(self) -> None:
        from app.providers.base import TranscriptionProvider

        provider = WhisperAPIProvider(api_key="sk-test")
        assert isinstance(provider, TranscriptionProvider)
