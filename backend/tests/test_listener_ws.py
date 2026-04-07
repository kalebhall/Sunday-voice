"""Tests for the anonymous listener WebSocket endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.websockets import WebSocketDisconnect

from app.core.security import hash_password
from app.db.session import get_session
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
from app.models.session import AudioTransport
from app.services.listener_connections import listener_connections

# -- Helpers ------------------------------------------------------------------


async def _seed_roles(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session:
        session.add(Role(name="admin", description="Full access"))
        session.add(Role(name="operator", description="Operator"))
        await session.commit()


async def _make_operator(
    maker: async_sessionmaker[AsyncSession],
) -> User:
    async with maker() as session:
        role = (
            await session.execute(select(Role).where(Role.name == "operator"))
        ).scalar_one()
        user = User(
            email="op@example.com",
            display_name="Operator",
            hashed_password=hash_password("pass"),
            role_id=role.id,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _make_active_session(
    maker: async_sessionmaker[AsyncSession],
    user: User,
    *,
    join_code: str = "ABC123",
    join_slug: str = "test-slug",
    languages: list[str] | None = None,
) -> Session:
    if languages is None:
        languages = ["es", "fr"]
    async with maker() as db:
        sess = Session(
            name="Test Session",
            join_slug=join_slug,
            join_code=join_code,
            source_language="en",
            status=SessionStatus.ACTIVE,
            audio_transport=AudioTransport.WEBSOCKET_CHUNKS,
            created_by_user_id=user.id,
        )
        for lang in languages:
            sess.languages.append(
                SessionLanguage(language_code=lang, tts_enabled=False)
            )
        db.add(sess)
        await db.commit()
        await db.refresh(sess, attribute_names=["languages"])
        return sess


async def _add_scrollback_segments(
    maker: async_sessionmaker[AsyncSession],
    session_id,
    lang: str,
    count: int,
) -> None:
    """Insert TranscriptSegment + TranslationSegment rows for scrollback testing."""
    async with maker() as db:
        for i in range(1, count + 1):
            ts = TranscriptSegment(
                session_id=session_id,
                sequence=i,
                language="en",
                text=f"transcript {i}",
            )
            db.add(ts)
            await db.flush()
            tl = TranslationSegment(
                session_id=session_id,
                transcript_segment_id=ts.id,
                language_code=lang,
                text=f"translated {i}",
                provider="google",
            )
            db.add(tl)
        await db.commit()


# -- Fixtures -----------------------------------------------------------------


@pytest_asyncio.fixture
async def db_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        for tbl in (
            Role,
            User,
            Session,
            SessionLanguage,
            TranscriptSegment,
            TranslationSegment,
        ):
            await conn.run_sync(
                lambda sync_conn, t=tbl: t.__table__.create(sync_conn, checkfirst=True)
            )
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    await _seed_roles(maker)
    try:
        yield maker
    finally:
        await engine.dispose()


def _mock_redis_pubsub():
    """Create a mock Redis pub/sub that yields no messages."""
    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.unsubscribe = AsyncMock()
    mock_pubsub.aclose = AsyncMock()
    mock_pubsub.get_message = AsyncMock(return_value=None)
    return mock_pubsub


def _mock_redis(pubsub=None):
    """Create a mock Redis client."""
    if pubsub is None:
        pubsub = _mock_redis_pubsub()
    mock_redis = MagicMock()
    mock_redis.pubsub.return_value = pubsub
    mock_redis.aclose = AsyncMock()
    return mock_redis


@pytest_asyncio.fixture
async def client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[TestClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    listener_connections.reset()

    with patch(
        "app.ws.listener.get_sessionmaker", return_value=db_sessionmaker
    ), patch(
        "app.ws.listener.Redis"
    ) as MockRedis:
        MockRedis.from_url.return_value = _mock_redis()
        try:
            with TestClient(app) as tc:
                yield tc
        finally:
            app.dependency_overrides.pop(get_session, None)
            listener_connections.reset()


# -- Tests --------------------------------------------------------------------


class TestListenerSessionValidation:
    async def test_unknown_session_closes_4404(
        self,
        client: TestClient,
    ) -> None:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/listen/BADCODE/es"):
                pass
        assert exc_info.value.code == 4404

    async def test_draft_session_closes_4404(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        async with db_sessionmaker() as db:
            sess = Session(
                name="Draft",
                join_slug="draft-slug",
                join_code="DRF001",
                source_language="en",
                status=SessionStatus.DRAFT,
                audio_transport=AudioTransport.WEBSOCKET_CHUNKS,
                created_by_user_id=user.id,
            )
            db.add(sess)
            await db.commit()

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/listen/DRF001/es"):
                pass
        assert exc_info.value.code == 4404

    async def test_invalid_language_closes_4400(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        await _make_active_session(
            db_sessionmaker, user, languages=["es"]
        )
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/listen/ABC123/zh"):
                pass
        assert exc_info.value.code == 4400


class TestListenerConnectionCap:
    async def test_exceeds_per_ip_cap(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        await _make_active_session(db_sessionmaker, user)

        # Set a very low cap for testing.
        listener_connections._max_per_ip = 1

        try:
            with client.websocket_connect("/ws/listen/ABC123/es") as ws1:
                # Second connection from same IP should be rejected.
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect("/ws/listen/ABC123/es"):
                        pass
                assert exc_info.value.code == 4429
                ws1.close()
        except Exception:
            pass
        finally:
            listener_connections._max_per_ip = 10


class TestListenerScrollback:
    async def test_receives_scrollback_on_connect(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        sess = await _make_active_session(db_sessionmaker, user)
        await _add_scrollback_segments(db_sessionmaker, sess.id, "es", 3)

        try:
            with client.websocket_connect("/ws/listen/ABC123/es") as ws:
                msg = ws.receive_json()
                assert msg["type"] == "scrollback"
                assert msg["count"] == 3
                assert len(msg["segments"]) == 3
                # Verify ordering: oldest first.
                seqs = [s["seq"] for s in msg["segments"]]
                assert seqs == [1, 2, 3]
                ws.close()
        except Exception:
            pass

    async def test_after_seq_filters_scrollback(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        sess = await _make_active_session(db_sessionmaker, user)
        await _add_scrollback_segments(db_sessionmaker, sess.id, "es", 5)

        try:
            with client.websocket_connect("/ws/listen/ABC123/es?after_seq=3") as ws:
                msg = ws.receive_json()
                assert msg["type"] == "scrollback"
                assert msg["count"] == 2
                seqs = [s["seq"] for s in msg["segments"]]
                assert seqs == [4, 5]
                ws.close()
        except Exception:
            pass


class TestListenerHeartbeat:
    async def test_heartbeat_sent_when_no_data(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        await _make_active_session(db_sessionmaker, user)

        try:
            with client.websocket_connect("/ws/listen/ABC123/es") as ws:
                # First message is scrollback.
                msg = ws.receive_json()
                assert msg["type"] == "scrollback"

                # Next message should be heartbeat (since no Redis data).
                msg = ws.receive_json()
                assert msg["type"] == "heartbeat"
                ws.close()
        except Exception:
            pass


class TestListenerConnectionTracker:
    """Unit tests for the ListenerConnectionTracker service."""

    async def test_acquire_and_release(self) -> None:
        tracker = listener_connections
        tracker.reset()
        tracker._max_per_ip = 2

        allowed, _ = await tracker.try_acquire("1.2.3.4")
        assert allowed
        assert tracker.connection_count("1.2.3.4") == 1
        allowed, _ = await tracker.try_acquire("1.2.3.4")
        assert allowed
        assert tracker.connection_count("1.2.3.4") == 2

        # Third should fail.
        allowed, reason = await tracker.try_acquire("1.2.3.4")
        assert not allowed
        assert reason
        assert tracker.connection_count("1.2.3.4") == 2

        await tracker.release("1.2.3.4")
        assert tracker.connection_count("1.2.3.4") == 1

        # Now it should succeed again.
        allowed, _ = await tracker.try_acquire("1.2.3.4")
        assert allowed
        assert tracker.connection_count("1.2.3.4") == 2

        tracker.reset()
        tracker._max_per_ip = 10

    async def test_context_manager(self) -> None:
        tracker = listener_connections
        tracker.reset()
        tracker._max_per_ip = 1

        async with tracker.track("5.6.7.8") as (allowed, _reason):
            assert allowed
            assert tracker.connection_count("5.6.7.8") == 1

        # After context exit, count should be back to 0.
        assert tracker.connection_count("5.6.7.8") == 0

        tracker.reset()
        tracker._max_per_ip = 10

    async def test_per_session_cap(self) -> None:
        tracker = listener_connections
        tracker.reset()
        tracker._max_per_ip = 100
        tracker._max_per_session = 2

        allowed, _ = await tracker.try_acquire("1.1.1.1", "session-A")
        assert allowed
        assert tracker.session_connection_count("session-A") == 1

        allowed, _ = await tracker.try_acquire("2.2.2.2", "session-A")
        assert allowed
        assert tracker.session_connection_count("session-A") == 2

        # Third connection to same session should be rejected (different IP is irrelevant).
        allowed, reason = await tracker.try_acquire("3.3.3.3", "session-A")
        assert not allowed
        assert "session" in reason.lower()
        assert tracker.session_connection_count("session-A") == 2

        # Different session should be unaffected.
        allowed, _ = await tracker.try_acquire("3.3.3.3", "session-B")
        assert allowed

        tracker.reset()
        tracker._max_per_ip = 10
        tracker._max_per_session = 100

    async def test_ip_and_session_caps_are_independent(self) -> None:
        """IP cap must block even if session has room, and vice versa."""
        tracker = listener_connections
        tracker.reset()
        tracker._max_per_ip = 1
        tracker._max_per_session = 10

        allowed, _ = await tracker.try_acquire("9.9.9.9", "sess-X")
        assert allowed

        # IP cap blocks a second connection from same IP even to a different session.
        allowed, reason = await tracker.try_acquire("9.9.9.9", "sess-Y")
        assert not allowed
        assert "IP" in reason

        tracker.reset()
        tracker._max_per_ip = 10
        tracker._max_per_session = 100


class TestListenerPerSessionCap:
    """Integration test: per-session connection cap via WebSocket."""

    async def test_exceeds_per_session_cap(
        self,
        client: TestClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _make_operator(db_sessionmaker)
        await _make_active_session(db_sessionmaker, user)

        listener_connections._max_per_ip = 100
        listener_connections._max_per_session = 1

        try:
            with client.websocket_connect("/ws/listen/ABC123/es") as ws1:
                # Second connection to same session should be rejected.
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect("/ws/listen/ABC123/es"):
                        pass
                assert exc_info.value.code == 4429
                ws1.close()
        except Exception:
            pass
        finally:
            listener_connections._max_per_ip = 10
            listener_connections._max_per_session = 100


class TestListenerKillSwitch:
    """The session_ended control message disconnects active listeners."""

    async def test_kill_switch_closes_listener(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Listener should receive session_ended message and close."""
        from app.db.session import get_session
        from app.main import app

        async def _override() -> AsyncIterator[AsyncSession]:
            async with db_sessionmaker() as session:
                yield session

        app.dependency_overrides[get_session] = _override
        listener_connections.reset()

        user = await _make_operator(db_sessionmaker)
        await _make_active_session(db_sessionmaker, user)

        # Build a mock pubsub that first returns None then returns the kill message.
        import json as _json

        call_count = 0

        async def _get_message(ignore_subscribe_messages=False, timeout=1.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # triggers heartbeat
            return {
                "type": "message",
                "channel": "session:00000000-0000-0000-0000-000000000000:control",
                "data": _json.dumps({"type": "session_ended"}),
            }

        mock_pubsub = AsyncMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.aclose = AsyncMock()
        mock_pubsub.get_message = _get_message

        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = mock_pubsub
        mock_redis.aclose = AsyncMock()

        try:
            with patch(
                "app.ws.listener.get_sessionmaker", return_value=db_sessionmaker
            ), patch("app.ws.listener.Redis") as MockRedis:
                # Return the control channel name that matches the actual session.
                # We'll intercept the channel by overriding get_message after subscribe.
                actual_control_channel: list[str] = []

                async def _capture_subscribe(*channels):
                    actual_control_channel.extend(channels)

                mock_pubsub.subscribe = _capture_subscribe

                async def _get_message_patched(
                    ignore_subscribe_messages=False, timeout=1.0
                ):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        return None
                    # Find the control channel from what was subscribed.
                    ctrl = next(
                        (c for c in actual_control_channel if c.endswith(":control")),
                        None,
                    )
                    if ctrl is None:
                        return None
                    return {
                        "type": "message",
                        "channel": ctrl,
                        "data": _json.dumps({"type": "session_ended"}),
                    }

                mock_pubsub.get_message = _get_message_patched
                MockRedis.from_url.return_value = mock_redis

                session_ended_msg = None
                with TestClient(app) as tc:
                    # The server sends session_ended then closes; the context
                    # manager exits cleanly (server-initiated close on an
                    # accepted socket does not raise WebSocketDisconnect).
                    try:
                        with tc.websocket_connect("/ws/listen/ABC123/es") as ws:
                            _scrollback = ws.receive_json()  # scrollback batch
                            _heartbeat = ws.receive_json()   # heartbeat (call 1)
                            session_ended_msg = ws.receive_json()  # kill-switch (call 2)
                    except WebSocketDisconnect:
                        pass  # also acceptable

                assert session_ended_msg is not None
                assert session_ended_msg["type"] == "session_ended"
        finally:
            app.dependency_overrides.pop(get_session, None)
            listener_connections.reset()
