"""Unit tests for the TranslationFanout service."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models.segment import TranscriptSegment, TranslationSegment
from app.models.session import Session, SessionLanguage, SessionStatus
from app.models.role import Role
from app.models.user import User
from app.services.pubsub import TranscriptEvent, transcript_pubsub
from app.services.translation import TranslationFanout


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


class FakeTranslationProvider:
    """In-memory translation provider that returns predictable translations."""

    def __init__(self, prefix: str = "translated") -> None:
        self._prefix = prefix
        self.calls: list[tuple[str, str, str]] = []

    async def translate(self, text: str, source_language: str, target_language: str) -> str:
        self.calls.append((text, source_language, target_language))
        return f"[{target_language}] {self._prefix}: {text}"


class FakeRedis:
    """Minimal Redis mock supporting publish and subscribe."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_maker() -> async_sessionmaker[AsyncSession]:
    """In-memory SQLite DB with required tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        for model in (Role, User, Session, SessionLanguage, TranscriptSegment, TranslationSegment):
            await conn.run_sync(
                lambda sync_conn, m=model: m.__table__.create(sync_conn, checkfirst=True)
            )

    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    # Seed role + user for FK constraints.
    async with maker() as db:
        role = Role(name="operator", description="Operator role")
        db.add(role)
        await db.flush()

        user = User(
            email="test@example.com",
            hashed_password="fakehash",
            display_name="Test",
            role_id=role.id,
        )
        db.add(user)
        await db.commit()

    return maker


@pytest_asyncio.fixture
async def session_id(db_maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """Create a session with Spanish and Tagalog target languages."""
    sid = uuid.uuid4()
    async with db_maker() as db:
        # Get user id
        from sqlalchemy import select as sel
        user_id = (await db.execute(sel(User.id))).scalar_one()

        session = Session(
            id=sid,
            join_slug=f"test-{sid.hex[:8]}",
            name="Test Session",
            source_language="en",
            status=SessionStatus.ACTIVE,
            created_by_user_id=user_id,
        )
        db.add(session)
        await db.flush()

        db.add(SessionLanguage(session_id=sid, language_code="es"))
        db.add(SessionLanguage(session_id=sid, language_code="tl"))
        await db.commit()

    return sid


async def _add_transcript_segment(
    db_maker: async_sessionmaker[AsyncSession],
    session_id: uuid.UUID,
    sequence: int,
    text: str,
) -> int:
    """Insert a TranscriptSegment and return its id."""
    async with db_maker() as db:
        seg = TranscriptSegment(
            session_id=session_id,
            sequence=sequence,
            language="en",
            text=text,
        )
        db.add(seg)
        await db.commit()
        await db.refresh(seg)
        return seg.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTranslationFanout:
    """Integration tests for the fanout service."""

    @pytest.mark.asyncio
    async def test_translates_to_all_target_languages(
        self,
        db_maker: async_sessionmaker[AsyncSession],
        session_id: uuid.UUID,
    ) -> None:
        seg_id = await _add_transcript_segment(db_maker, session_id, 1, "Good morning")

        provider = FakeTranslationProvider()
        redis = FakeRedis()

        fanout = TranslationFanout(
            translation_provider=provider,
            db_sessionmaker=db_maker,
            redis=redis,  # type: ignore[arg-type]
        )

        await fanout.start(session_id)
        # Allow the consumer to subscribe.
        await asyncio.sleep(0.05)

        # Publish a transcript event.
        event = TranscriptEvent(
            session_id=session_id,
            sequence=1,
            language="en",
            text="Good morning",
        )
        await transcript_pubsub.publish(event)

        # Give the fanout time to process.
        await asyncio.sleep(0.2)
        await fanout.stop(session_id)

        # Provider was called for es and tl (not en, since that's the source).
        assert len(provider.calls) == 2
        target_langs = {call[2] for call in provider.calls}
        assert target_langs == {"es", "tl"}

    @pytest.mark.asyncio
    async def test_persists_translation_segments(
        self,
        db_maker: async_sessionmaker[AsyncSession],
        session_id: uuid.UUID,
    ) -> None:
        seg_id = await _add_transcript_segment(db_maker, session_id, 1, "Hello")

        provider = FakeTranslationProvider()
        redis = FakeRedis()

        fanout = TranslationFanout(
            translation_provider=provider,
            db_sessionmaker=db_maker,
            redis=redis,  # type: ignore[arg-type]
        )

        await fanout.start(session_id)
        await asyncio.sleep(0.05)

        event = TranscriptEvent(
            session_id=session_id, sequence=1, language="en", text="Hello"
        )
        await transcript_pubsub.publish(event)
        await asyncio.sleep(0.2)
        await fanout.stop(session_id)

        # Verify rows in DB.
        async with db_maker() as db:
            result = await db.execute(
                select(TranslationSegment).where(
                    TranslationSegment.session_id == session_id
                )
            )
            translations = result.scalars().all()

        assert len(translations) == 2
        lang_codes = {t.language_code for t in translations}
        assert lang_codes == {"es", "tl"}

        for t in translations:
            assert t.provider == "google"
            assert t.transcript_segment_id == seg_id

    @pytest.mark.asyncio
    async def test_publishes_to_redis(
        self,
        db_maker: async_sessionmaker[AsyncSession],
        session_id: uuid.UUID,
    ) -> None:
        await _add_transcript_segment(db_maker, session_id, 1, "Hello")

        provider = FakeTranslationProvider()
        redis = FakeRedis()

        fanout = TranslationFanout(
            translation_provider=provider,
            db_sessionmaker=db_maker,
            redis=redis,  # type: ignore[arg-type]
        )

        await fanout.start(session_id)
        await asyncio.sleep(0.05)

        event = TranscriptEvent(
            session_id=session_id, sequence=1, language="en", text="Hello"
        )
        await transcript_pubsub.publish(event)
        await asyncio.sleep(0.2)
        await fanout.stop(session_id)

        # Should have published to two channels.
        assert len(redis.published) == 2
        channels = {ch for ch, _ in redis.published}
        assert f"session:{session_id}:lang:es" in channels
        assert f"session:{session_id}:lang:tl" in channels

        # Verify payload structure.
        for channel, msg in redis.published:
            data = json.loads(msg)
            assert data["session_id"] == str(session_id)
            assert data["sequence"] == 1
            assert data["source_language"] == "en"
            assert data["language"] in ("es", "tl")
            assert "translated:" in data["text"]

    @pytest.mark.asyncio
    async def test_skips_source_language(
        self,
        db_maker: async_sessionmaker[AsyncSession],
        session_id: uuid.UUID,
    ) -> None:
        """If source language is in the target list, it is skipped."""
        # Add "en" as a target language too.
        async with db_maker() as db:
            db.add(SessionLanguage(session_id=session_id, language_code="en"))
            await db.commit()

        await _add_transcript_segment(db_maker, session_id, 1, "Hello")

        provider = FakeTranslationProvider()
        redis = FakeRedis()

        fanout = TranslationFanout(
            translation_provider=provider,
            db_sessionmaker=db_maker,
            redis=redis,  # type: ignore[arg-type]
        )

        await fanout.start(session_id)
        await asyncio.sleep(0.05)

        event = TranscriptEvent(
            session_id=session_id, sequence=1, language="en", text="Hello"
        )
        await transcript_pubsub.publish(event)
        await asyncio.sleep(0.2)
        await fanout.stop(session_id)

        # "en" should NOT be in the translated languages.
        target_langs = {call[2] for call in provider.calls}
        assert "en" not in target_langs
        assert target_langs == {"es", "tl"}

    @pytest.mark.asyncio
    async def test_translation_error_does_not_crash_fanout(
        self,
        db_maker: async_sessionmaker[AsyncSession],
        session_id: uuid.UUID,
    ) -> None:
        """A single translation failure shouldn't stop the fanout loop."""
        await _add_transcript_segment(db_maker, session_id, 1, "Hello")
        await _add_transcript_segment(db_maker, session_id, 2, "World")

        call_count = 0

        class FailOnceThenSucceed:
            async def translate(self, text: str, source_language: str, target_language: str) -> str:
                nonlocal call_count
                call_count += 1
                if call_count <= 2:  # Fail on first event (2 languages)
                    raise RuntimeError("Simulated failure")
                return f"[{target_language}] {text}"

        redis = FakeRedis()
        fanout = TranslationFanout(
            translation_provider=FailOnceThenSucceed(),  # type: ignore[arg-type]
            db_sessionmaker=db_maker,
            redis=redis,  # type: ignore[arg-type]
        )

        await fanout.start(session_id)
        await asyncio.sleep(0.05)

        # First event — translations will fail.
        event1 = TranscriptEvent(
            session_id=session_id, sequence=1, language="en", text="Hello"
        )
        await transcript_pubsub.publish(event1)
        await asyncio.sleep(0.2)

        # Second event — should succeed.
        event2 = TranscriptEvent(
            session_id=session_id, sequence=2, language="en", text="World"
        )
        await transcript_pubsub.publish(event2)
        await asyncio.sleep(0.2)

        await fanout.stop(session_id)

        # Second event's translations should have been published.
        assert len(redis.published) == 2

    @pytest.mark.asyncio
    async def test_stop_all(
        self,
        db_maker: async_sessionmaker[AsyncSession],
        session_id: uuid.UUID,
    ) -> None:
        provider = FakeTranslationProvider()
        redis = FakeRedis()
        fanout = TranslationFanout(
            translation_provider=provider,
            db_sessionmaker=db_maker,
            redis=redis,  # type: ignore[arg-type]
        )

        await fanout.start(session_id)
        await asyncio.sleep(0.05)
        await fanout.stop_all()

        # Task should be cleaned up.
        assert len(fanout._tasks) == 0
