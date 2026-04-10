"""Unit tests for GoogleTTSProvider, TTSCache, and TTSService."""

from __future__ import annotations

import base64
import json
import tempfile
import time
from dataclasses import dataclass
from unittest.mock import MagicMock

import httpx
import pytest

from app.providers.google_tts import GoogleTTSError, GoogleTTSProvider
from app.services.tts import TTSCache, TTSService, cache_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class RecordedUsage:
    provider: str
    operation: str
    units: float


class FakeCostMeter:
    def __init__(self) -> None:
        self.records: list[RecordedUsage] = []

    async def record(self, provider: str, operation: str, units: float) -> None:
        self.records.append(RecordedUsage(provider, operation, units))


# ---------------------------------------------------------------------------
# Mock HTTP transport
# ---------------------------------------------------------------------------


FAKE_AUDIO = b"\xff\xfb\x90\x00" + b"\x00" * 100  # Fake MP3 bytes


def _tts_response(audio: bytes = FAKE_AUDIO, status: int = 200) -> httpx.Response:
    body = json.dumps({"audioContent": base64.b64encode(audio).decode()})
    return httpx.Response(status, text=body)


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# GoogleTTSProvider tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_success() -> None:
    transport = _MockTransport([_tts_response()])
    client = httpx.AsyncClient(transport=transport)
    meter = FakeCostMeter()

    provider = GoogleTTSProvider(
        access_token="test-token",
        cost_meter=meter,
        http_client=client,
    )

    audio = await provider.synthesize("Hola mundo", "es")

    assert audio == FAKE_AUDIO
    assert len(transport.requests) == 1

    # Verify cost metering.
    assert len(meter.records) == 1
    assert meter.records[0].provider == "google"
    assert meter.records[0].operation == "tts_char"
    assert meter.records[0].units == float(len("Hola mundo"))


@pytest.mark.asyncio
async def test_synthesize_retries_on_503() -> None:
    transport = _MockTransport([
        httpx.Response(503, text="Service Unavailable"),
        _tts_response(),
    ])
    client = httpx.AsyncClient(transport=transport)

    provider = GoogleTTSProvider(
        access_token="test-token",
        http_client=client,
        backoff_base=0.01,
    )

    audio = await provider.synthesize("Hello", "en")
    assert audio == FAKE_AUDIO
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_synthesize_fails_after_retries() -> None:
    transport = _MockTransport([
        httpx.Response(503, text="err"),
        httpx.Response(503, text="err"),
        httpx.Response(503, text="err"),
    ])
    client = httpx.AsyncClient(transport=transport)

    provider = GoogleTTSProvider(
        access_token="test-token",
        http_client=client,
        backoff_base=0.01,
    )

    with pytest.raises(GoogleTTSError, match="failed after 3 attempts"):
        await provider.synthesize("Hello", "en")


@pytest.mark.asyncio
async def test_synthesize_empty_audio_raises() -> None:
    body = json.dumps({"audioContent": ""})
    transport = _MockTransport([httpx.Response(200, text=body)])
    client = httpx.AsyncClient(transport=transport)

    provider = GoogleTTSProvider(
        access_token="test-token",
        http_client=client,
    )

    with pytest.raises(GoogleTTSError, match="Empty audioContent"):
        await provider.synthesize("Hello", "en")


# ---------------------------------------------------------------------------
# TTSCache tests
# ---------------------------------------------------------------------------


class TestTTSCache:
    def test_put_and_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = TTSCache(tmpdir, ttl_seconds=3600)
            key = cache_key("hello", "en")

            assert cache.get(key) is None

            cache.put(key, FAKE_AUDIO)
            assert cache.get(key) == FAKE_AUDIO

    def test_expired_entry_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = TTSCache(tmpdir, ttl_seconds=1)
            key = cache_key("hello", "en")

            cache.put(key, FAKE_AUDIO)
            assert cache.get(key) == FAKE_AUDIO

            # Manually backdate the meta file.
            from pathlib import Path
            meta = Path(tmpdir) / f"{key}.meta"
            meta.write_text(str(time.time() - 10))

            assert cache.get(key) is None

    def test_evict_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = TTSCache(tmpdir, ttl_seconds=1)
            key1 = cache_key("a", "en")
            key2 = cache_key("b", "en")

            cache.put(key1, FAKE_AUDIO)
            cache.put(key2, FAKE_AUDIO)

            # Backdate all entries.
            from pathlib import Path
            for meta in Path(tmpdir).glob("*.meta"):
                meta.write_text(str(time.time() - 10))

            removed = cache.evict_expired()
            assert removed == 2
            assert cache.get(key1) is None
            assert cache.get(key2) is None

    def test_cache_key_deterministic(self) -> None:
        k1 = cache_key("hello", "en", "voice-A")
        k2 = cache_key("hello", "en", "voice-A")
        k3 = cache_key("hello", "es", "voice-A")

        assert k1 == k2
        assert k1 != k3

    def test_content_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mp3_cache = TTSCache(tmpdir, ttl_seconds=3600, audio_encoding="MP3")
            assert mp3_cache.content_type == "audio/mpeg"

            ogg_cache = TTSCache(tmpdir, ttl_seconds=3600, audio_encoding="OGG_OPUS")
            assert ogg_cache.content_type == "audio/ogg"


# ---------------------------------------------------------------------------
# TTSService tests
# ---------------------------------------------------------------------------


class TestTTSServiceEviction:
    def test_evict_expired_delegates_to_cache(self) -> None:
        """TTSService.evict_expired() proxies to the underlying TTSCache."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = TTSCache(tmpdir, ttl_seconds=1)
            service = TTSService(
                provider=MagicMock(),
                cache=cache,
                db_sessionmaker=MagicMock(),
            )

            key = cache_key("hello", "en")
            cache.put(key, FAKE_AUDIO)

            # Backdate the meta file so the entry is expired.
            from pathlib import Path
            meta = Path(tmpdir) / f"{key}.meta"
            meta.write_text(str(time.time() - 10))

            removed = service.evict_expired()
            assert removed == 1
            assert cache.get(key) is None

    def test_evict_expired_returns_zero_when_nothing_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = TTSCache(tmpdir, ttl_seconds=3600)
            service = TTSService(
                provider=MagicMock(),
                cache=cache,
                db_sessionmaker=MagicMock(),
            )

            key = cache_key("hello", "en")
            cache.put(key, FAKE_AUDIO)

            assert service.evict_expired() == 0
