"""Tests for the WebRTC SDP offer endpoint and audio helpers."""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes.webrtc import (
    _pcm_to_wav,
)
from app.core.security import create_access_token, hash_password
from app.db.session import get_session
from app.main import app
from app.models import Role, Session, SessionLanguage, SessionStatus, User
from app.models.session import AudioTransport
from app.services.audio_ingest import reset_operator_locks

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
    transport: AudioTransport = AudioTransport.WEBRTC,
) -> Session:
    async with maker() as db:
        sess = Session(
            name="WebRTC Test Session",
            join_slug="webrtc-slug-" + uuid4().hex[:10],
            source_language="en",
            status=status,
            audio_transport=transport,
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
    async def _override() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    reset_operator_locks()
    try:
        with TestClient(app) as tc:
            yield tc
    finally:
        app.dependency_overrides.pop(get_session, None)
        reset_operator_locks()


# -- Unit tests: PCM-to-WAV encoder ------------------------------------------


class TestPcmToWav:
    def test_produces_valid_wav(self) -> None:
        """_pcm_to_wav should produce a valid WAV file."""
        # 1 second of silence at 16 kHz mono, 16-bit.
        num_samples = 16_000
        pcm = b"\x00\x00" * num_samples
        wav_bytes = _pcm_to_wav(pcm, sample_rate=16_000, channels=1)

        # Parse it back to verify.
        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16_000
            assert wf.getnframes() == num_samples

    def test_stereo_wav(self) -> None:
        pcm = b"\x00\x00\x00\x00" * 8_000  # 0.5s stereo 16 kHz
        wav_bytes = _pcm_to_wav(pcm, sample_rate=16_000, channels=2)
        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            assert wf.getnchannels() == 2
            assert wf.getnframes() == 8_000


# -- Integration tests: SDP offer endpoint -----------------------------------


class TestWebrtcOfferEndpoint:
    async def test_missing_auth_returns_401(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        sess = await _make_session(db_sessionmaker, user=user)
        resp = client.post(
            f"/api/sessions/{sess.id}/webrtc/offer",
            json={"sdp": "v=0\r\n", "type": "offer"},
        )
        assert resp.status_code == 401

    async def test_inactive_session_returns_404(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        sess = await _make_session(
            db_sessionmaker, user=user, status=SessionStatus.DRAFT
        )
        token = _token_for(user)
        resp = client.post(
            f"/api/sessions/{sess.id}/webrtc/offer",
            json={"sdp": "v=0\r\n", "type": "offer"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    async def test_unknown_session_returns_404(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        token = _token_for(user)
        fake_id = uuid4()
        resp = client.post(
            f"/api/sessions/{fake_id}/webrtc/offer",
            json={"sdp": "v=0\r\n", "type": "offer"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    async def test_sdp_exchange_returns_answer(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A valid offer should produce an SDP answer."""
        user = await _make_operator(db_sessionmaker)
        sess = await _make_session(db_sessionmaker, user=user)
        token = _token_for(user)

        fake_answer_sdp = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n"

        mock_pc = MagicMock()
        mock_pc.setRemoteDescription = AsyncMock()
        mock_pc.createAnswer = AsyncMock(
            return_value=MagicMock(sdp=fake_answer_sdp, type="answer")
        )
        mock_pc.setLocalDescription = AsyncMock()
        mock_pc.localDescription = MagicMock(
            sdp=fake_answer_sdp, type="answer"
        )
        mock_pc.on = MagicMock(side_effect=lambda event: lambda fn: fn)

        with patch(
            "app.api.routes.webrtc.RTCPeerConnection",
            return_value=mock_pc,
        ), patch(
            "app.services.audio_ingest.WhisperAPIProvider",
        ):
            resp = client.post(
                f"/api/sessions/{sess.id}/webrtc/offer",
                json={"sdp": "v=0\r\n", "type": "offer"},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "answer"
        assert body["sdp"] == fake_answer_sdp

    async def test_second_operator_gets_409(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Only one operator can connect via WebRTC per session."""
        from app.services.audio_ingest import acquire_operator_lock

        user = await _make_operator(db_sessionmaker)
        sess = await _make_session(db_sessionmaker, user=user)
        token = _token_for(user)

        # Pre-acquire the lock.
        assert await acquire_operator_lock(sess.id) is True

        resp = client.post(
            f"/api/sessions/{sess.id}/webrtc/offer",
            json={"sdp": "v=0\r\n", "type": "offer"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409
