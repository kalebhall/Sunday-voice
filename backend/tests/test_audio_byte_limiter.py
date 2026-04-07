"""Unit tests for AudioByteRateLimiter and reset_audio_byte_limiter."""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from app.services.audio_ingest import (
    AudioByteRateLimiter,
    get_audio_byte_limiter,
    reset_audio_byte_limiter,
)


@pytest.fixture(autouse=True)
def _reset_limiter():
    reset_audio_byte_limiter()
    yield
    reset_audio_byte_limiter()


class TestAudioByteRateLimiter:
    async def test_allows_within_cap(self) -> None:
        limiter = AudioByteRateLimiter(max_bytes_per_minute=1000)
        session_id = uuid4()

        assert await limiter.record_and_check(session_id, 400)
        assert await limiter.record_and_check(session_id, 400)
        # 800 total — still under 1000
        assert await limiter.record_and_check(session_id, 199)

    async def test_rejects_when_cap_exceeded(self) -> None:
        limiter = AudioByteRateLimiter(max_bytes_per_minute=500)
        session_id = uuid4()

        assert await limiter.record_and_check(session_id, 400)
        # 500 total exactly — should be rejected (> 500 is the trigger)
        assert not await limiter.record_and_check(session_id, 101)

    async def test_single_chunk_over_cap_rejected(self) -> None:
        limiter = AudioByteRateLimiter(max_bytes_per_minute=100)
        session_id = uuid4()

        assert not await limiter.record_and_check(session_id, 101)
        # No bytes should have been recorded on failure.
        assert await limiter.record_and_check(session_id, 100)

    async def test_sessions_are_independent(self) -> None:
        limiter = AudioByteRateLimiter(max_bytes_per_minute=100)
        s1, s2 = uuid4(), uuid4()

        assert await limiter.record_and_check(s1, 90)
        # s2 is unaffected by s1's usage.
        assert await limiter.record_and_check(s2, 100)

    async def test_old_entries_expire(self) -> None:
        limiter = AudioByteRateLimiter(max_bytes_per_minute=100)
        session_id = uuid4()

        # Inject a fake old timestamp so the entry falls outside the window.
        import time

        old_time = time.monotonic() - 61.0  # 61 s ago — outside 60 s window

        from collections import deque

        limiter._windows[session_id] = deque([(old_time, 99)])

        # Old entry should be evicted; new chunk of 100 is within cap.
        assert await limiter.record_and_check(session_id, 100)

    async def test_reset_clears_session(self) -> None:
        limiter = AudioByteRateLimiter(max_bytes_per_minute=10)
        session_id = uuid4()

        assert not await limiter.record_and_check(session_id, 11)
        limiter.reset(session_id)
        assert await limiter.record_and_check(session_id, 10)

    async def test_reset_all(self) -> None:
        limiter = AudioByteRateLimiter(max_bytes_per_minute=10)
        s1, s2 = uuid4(), uuid4()

        assert not await limiter.record_and_check(s1, 11)
        assert not await limiter.record_and_check(s2, 11)
        limiter.reset()
        assert await limiter.record_and_check(s1, 10)
        assert await limiter.record_and_check(s2, 10)


class TestGetAudioByteLimiter:
    async def test_returns_singleton(self) -> None:
        import os

        os.environ["OPERATOR_AUDIO_MAX_BYTES_PER_MINUTE"] = str(5 * 1024 * 1024)
        try:
            a = await get_audio_byte_limiter()
            b = await get_audio_byte_limiter()
            assert a is b
            assert a._max_bytes == 5 * 1024 * 1024
        finally:
            del os.environ["OPERATOR_AUDIO_MAX_BYTES_PER_MINUTE"]
            reset_audio_byte_limiter()
