"""Tests for GET /api/tts/{segment_id}."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token, hash_password
from app.main import app
from app.models import (
    Role,
    Session,
    SessionLanguage,
    SessionStatus,
    TranscriptSegment,
    TranslationSegment,
    User,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _make_operator(maker: async_sessionmaker[AsyncSession]) -> User:
    async with maker() as session:
        role = (
            await session.execute(select(Role).where(Role.name == "operator"))
        ).scalar_one()
        user = User(
            email="tts_op@test.com",
            display_name="TTS Operator",
            hashed_password=hash_password("securepass"),
            role_id=role.id,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _auth(user: User) -> dict[str, str]:
    tok = create_access_token(user_id=user.id, role=user.role.name)
    return {"Authorization": f"Bearer {tok}"}


@pytest_asyncio.fixture
async def active_session_with_segment(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[Session, TranslationSegment]:
    """Create an ACTIVE session with one transcript+translation segment pair."""
    op = await _make_operator(db_sessionmaker)
    async with db_sessionmaker() as db:
        sess = Session(
            id=uuid.uuid4(),
            name="TTS Test",
            join_slug="tts-slug",
            join_code="TTSC01",
            source_language="en",
            status=SessionStatus.ACTIVE,
            created_by_user_id=op.id,
        )
        db.add(sess)
        await db.flush()

        transcript_seg = TranscriptSegment(
            session_id=sess.id,
            sequence=1,
            language="en",
            text="Hello world",
        )
        db.add(transcript_seg)
        await db.flush()

        translation_seg = TranslationSegment(
            session_id=sess.id,
            transcript_segment_id=transcript_seg.id,
            language_code="es",
            text="Hola mundo",
            provider="google",
        )
        db.add(translation_seg)
        await db.commit()
        await db.refresh(translation_seg)
        return sess, translation_seg


@pytest_asyncio.fixture
async def ended_session_with_segment(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[Session, TranslationSegment]:
    """Create an ENDED session with one translation segment."""
    op = await _make_operator(db_sessionmaker)
    async with db_sessionmaker() as db:
        sess = Session(
            id=uuid.uuid4(),
            name="Ended TTS",
            join_slug="ended-slug",
            join_code="ENDC01",
            source_language="en",
            status=SessionStatus.ENDED,
            created_by_user_id=op.id,
        )
        db.add(sess)
        await db.flush()

        transcript_seg = TranscriptSegment(
            session_id=sess.id,
            sequence=1,
            language="en",
            text="Goodbye",
        )
        db.add(transcript_seg)
        await db.flush()

        translation_seg = TranslationSegment(
            session_id=sess.id,
            transcript_segment_id=transcript_seg.id,
            language_code="es",
            text="Adiós",
            provider="google",
        )
        db.add(translation_seg)
        await db.commit()
        await db.refresh(translation_seg)
        return sess, translation_seg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tts_503_when_service_not_configured(client: TestClient) -> None:
    # TTS_ENABLED is false in the test environment (set in conftest), so
    # app.state.tts_service is never set during lifespan startup.
    resp = client.get("/api/tts/1")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "TTS service not available"


def test_tts_404_for_nonexistent_segment(client: TestClient) -> None:
    mock_tts = MagicMock()
    app.state.tts_service = mock_tts
    try:
        resp = client.get("/api/tts/999999")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "audio not available"
    finally:
        del app.state.tts_service


def test_tts_404_for_ended_session_segment(
    client: TestClient,
    ended_session_with_segment: tuple[Session, TranslationSegment],
) -> None:
    _, seg = ended_session_with_segment
    mock_tts = MagicMock()
    app.state.tts_service = mock_tts
    try:
        resp = client.get(f"/api/tts/{seg.id}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "audio not available"
        # The TTS service should NOT have been called — session-isolation guard fired first.
        mock_tts.get_audio_for_segment.assert_not_called()
    finally:
        del app.state.tts_service


def test_tts_returns_audio_for_active_session_segment(
    client: TestClient,
    active_session_with_segment: tuple[Session, TranslationSegment],
) -> None:
    _, seg = active_session_with_segment
    mock_tts = MagicMock()
    mock_tts.get_audio_for_segment = AsyncMock(return_value=(b"audio-bytes", "audio/mpeg"))
    app.state.tts_service = mock_tts
    try:
        resp = client.get(f"/api/tts/{seg.id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mpeg"
        assert resp.content == b"audio-bytes"
        assert "max-age=3600" in resp.headers["cache-control"]
    finally:
        del app.state.tts_service


def test_tts_404_when_synthesis_not_ready(
    client: TestClient,
    active_session_with_segment: tuple[Session, TranslationSegment],
) -> None:
    _, seg = active_session_with_segment
    mock_tts = MagicMock()
    mock_tts.get_audio_for_segment = AsyncMock(return_value=(None, "audio/mpeg"))
    app.state.tts_service = mock_tts
    try:
        resp = client.get(f"/api/tts/{seg.id}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "audio not available"
    finally:
        del app.state.tts_service
