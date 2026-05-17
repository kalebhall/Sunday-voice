"""Unit tests for audio-ingest transcript sequence seeding."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.segment import TranscriptSegment
from app.services import audio_ingest


@pytest_asyncio.fixture
async def seq_sessionmaker(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory DB with a transcript_segments table, wired into audio_ingest."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: TranscriptSegment.__table__.create(c, checkfirst=True)
        )
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(audio_ingest, "get_sessionmaker", lambda: maker)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_starting_sequence_empty_session(
    seq_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A session with no transcript segments starts numbering at 0."""
    assert await audio_ingest.starting_sequence(uuid.uuid4()) == 0


@pytest.mark.asyncio
async def test_starting_sequence_continues_after_reconnect(
    seq_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A reconnecting operator continues numbering instead of restarting at 1.

    Restarting at 1 would collide with the unique (session_id, sequence)
    constraint on segments persisted by the previous connection.
    """
    session_id = uuid.uuid4()
    async with seq_sessionmaker() as db:
        for seq in range(1, 6):
            db.add(
                TranscriptSegment(
                    session_id=session_id,
                    sequence=seq,
                    language="en",
                    text=f"segment {seq}",
                )
            )
        await db.commit()

    assert await audio_ingest.starting_sequence(session_id) == 5
    # A different session is unaffected by another session's segments.
    assert await audio_ingest.starting_sequence(uuid.uuid4()) == 0


@pytest.mark.asyncio
async def test_starting_sequence_degrades_to_zero_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the lookup fails, fall back to 0 rather than crashing the task."""

    def _broken_sessionmaker() -> object:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(audio_ingest, "get_sessionmaker", _broken_sessionmaker)
    assert await audio_ingest.starting_sequence(uuid.uuid4()) == 0
