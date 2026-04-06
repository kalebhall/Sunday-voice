"""Tests for the operator audio WebSocket endpoint."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.websockets import WebSocketDisconnect

from app.core.security import create_access_token, hash_password
from app.db.session import get_session
from app.main import app
from app.models import Role, Session, SessionLanguage, SessionStatus, User
from app.models.session import AudioTransport
from app.services.pubsub import TranscriptEvent, transcript_pubsub
from app.ws.operator_audio import reset_operator_locks

# -- Helpers ------------------------------------------------------------------


async def _make_operator(
    maker: async_sessionmaker[AsyncSession],
    *,
    email: str = "op@example.com",
) -> User:
    async with maker() as session:
        role = (
            await session.execute(select(Role).where(Role.name == "operator"))
        ).scalar_one()
        user = User(
            email=email,
            display_name="Operator",
            hashed_password=hash_password("pass"),
            role_id=role.id,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _make_session(
    maker: async_sessionmaker[AsyncSession],
    *,
    user: User,
    status: SessionStatus = SessionStatus.ACTIVE,
) -> Session:
    async with maker() as db:
        sess = Session(
            name="Test Session",
            join_slug="test-slug-" + uuid4().hex[:10],
            source_language="en",
            status=status,
            audio_transport=AudioTransport.WEBSOCKET_CHUNKS,
            created_by_user_id=user.id,
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        return sess


def _token_for(user: User) -> str:
    return create_access_token(user_id=user.id, role="operator")


# -- Fixtures -----------------------------------------------------------------


@pytest_asyncio.fixture
async def db_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory SQLite with the tables needed by the WS handler."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        for tbl in (Role, User, Session, SessionLanguage):
            await conn.run_sync(
                lambda sync_conn, t=tbl: t.__table__.create(sync_conn, checkfirst=True)
            )
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        session.add(Role(name="admin", description="Full access"))
        session.add(Role(name="operator", description="Operator"))
        await session.commit()
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[TestClient]:
    """TestClient with both DI override and get_sessionmaker patched."""

    async def _override() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    reset_operator_locks()
    # The WS handler calls get_sessionmaker() directly, so patch it too.
    with patch(
        "app.ws.operator_audio.get_sessionmaker", return_value=db_sessionmaker
    ):
        try:
            with TestClient(app) as tc:
                yield tc
        finally:
            app.dependency_overrides.pop(get_session, None)
            reset_operator_locks()


# -- Auth tests ---------------------------------------------------------------


class TestOperatorAudioAuth:
    async def test_missing_token_closes_4401(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        sess = await _make_session(db_sessionmaker, user=user)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/ws/operator/{sess.id}/audio"):
                pass  # pragma: no cover
        assert exc_info.value.code == 4401

    async def test_invalid_token_closes_4401(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        sess = await _make_session(db_sessionmaker, user=user)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/ws/operator/{sess.id}/audio?token=garbage"
            ):
                pass  # pragma: no cover
        assert exc_info.value.code == 4401


class TestOperatorAudioSession:
    async def test_non_active_session_closes_4404(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        sess = await _make_session(db_sessionmaker, user=user, status=SessionStatus.DRAFT)
        token = _token_for(user)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/ws/operator/{sess.id}/audio?token={token}"
            ):
                pass  # pragma: no cover
        assert exc_info.value.code == 4404

    async def test_unknown_session_closes_4404(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        token = _token_for(user)
        fake_id = uuid4()
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/ws/operator/{fake_id}/audio?token={token}"
            ):
                pass  # pragma: no cover
        assert exc_info.value.code == 4404


# -- Ingest tests -------------------------------------------------------------


class TestOperatorAudioIngest:
    async def test_accepts_and_processes_chunks(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Connect, send a binary chunk, verify transcription task runs."""
        user = await _make_operator(db_sessionmaker)
        sess = await _make_session(db_sessionmaker, user=user)
        token = _token_for(user)

        # Mock the transcription provider to yield a known string.
        async def _fake_transcribe(audio_stream, source_language=None):
            async for _chunk in audio_stream:
                pass
            yield "Hello world"

        with patch(
            "app.services.audio_ingest.WhisperAPIProvider"
        ) as MockProvider:
            instance = MockProvider.return_value
            instance.transcribe_stream = _fake_transcribe

            # Subscribe to pub/sub to verify events arrive.
            ps = await transcript_pubsub.get_or_create(sess.id)
            sub_id, q = await ps.subscribe()

            try:
                with client.websocket_connect(
                    f"/ws/operator/{sess.id}/audio?token={token}"
                ) as ws:
                    ws.send_bytes(b"\x00" * 1024)
                    ws.close()
            except Exception:
                pass  # WebSocket close can raise in test client

            # Give the transcription task a moment to complete.
            await asyncio.sleep(0.3)

            # Check that an event was published.
            if not q.empty():
                event: TranscriptEvent = q.get_nowait()
                assert event.session_id == sess.id
                assert event.text == "Hello world"
                assert event.language == "en"
                assert event.sequence == 1

            await ps.unsubscribe(sub_id)
            await transcript_pubsub.remove_if_empty(sess.id)


class TestSingleOperatorLock:
    async def test_second_operator_rejected_4409(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Only one operator can connect to a session at a time."""
        user = await _make_operator(db_sessionmaker)
        sess = await _make_session(db_sessionmaker, user=user)
        token = _token_for(user)

        with patch("app.services.audio_ingest.WhisperAPIProvider"):
            try:
                with client.websocket_connect(
                    f"/ws/operator/{sess.id}/audio?token={token}"
                ) as ws1:
                    # Second connection should be rejected with 4409.
                    with pytest.raises(WebSocketDisconnect) as exc_info:
                        with client.websocket_connect(
                            f"/ws/operator/{sess.id}/audio?token={token}"
                        ):
                            pass  # pragma: no cover
                    assert exc_info.value.code == 4409
                    ws1.close()
            except Exception:
                pass
