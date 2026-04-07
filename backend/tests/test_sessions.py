"""End-to-end tests for /api/sessions CRUD and anonymous join."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import reset_join_rate_limiter
from app.core.security import create_access_token, hash_password
from app.models import Role, User

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _make_user(
    maker: async_sessionmaker[AsyncSession],
    *,
    email: str,
    password: str,
    role_name: str,
) -> User:
    async with maker() as session:
        role = (
            await session.execute(select(Role).where(Role.name == role_name))
        ).scalar_one()
        user = User(
            email=email,
            display_name="Test",
            hashed_password=hash_password(password),
            role_id=role.id,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest_asyncio.fixture
async def operator(db_sessionmaker: async_sessionmaker[AsyncSession]) -> User:
    return await _make_user(db_sessionmaker, email="op@test.com", password="pw", role_name="operator")


@pytest_asyncio.fixture
async def admin(db_sessionmaker: async_sessionmaker[AsyncSession]) -> User:
    return await _make_user(db_sessionmaker, email="admin@test.com", password="pw", role_name="admin")


@pytest_asyncio.fixture
async def operator2(db_sessionmaker: async_sessionmaker[AsyncSession]) -> User:
    return await _make_user(db_sessionmaker, email="op2@test.com", password="pw", role_name="operator")


def _auth(user: User) -> dict[str, str]:
    tok = create_access_token(user_id=user.id, role=user.role.name)
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
# POST /api/sessions  (create)
# ---------------------------------------------------------------------------


def test_create_session_minimal(client: TestClient, operator: User) -> None:
    resp = client.post(
        "/api/sessions",
        json={"name": "Sacrament Meeting"},
        headers=_auth(operator),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Sacrament Meeting"
    assert body["source_language"] == "en"
    assert body["status"] == "draft"
    assert body["audio_transport"] == "websocket_chunks"
    assert body["join_code"] is not None
    assert len(body["join_code"]) == 6
    assert body["join_slug"]
    assert "/join/" in body["join_url"]
    assert body["target_languages"] == []
    assert body["scheduled_at"] is None
    assert body["created_by_user_id"] == operator.id


def test_create_session_full(client: TestClient, operator: User) -> None:
    resp = client.post(
        "/api/sessions",
        json={
            "name": "Sunday School",
            "source_language": "en",
            "audio_transport": "webrtc",
            "target_languages": [
                {"language_code": "es", "tts_enabled": True},
                {"language_code": "sm", "tts_enabled": False},
            ],
            "scheduled_at": "2026-04-06T10:00:00Z",
        },
        headers=_auth(operator),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["audio_transport"] == "webrtc"
    assert len(body["target_languages"]) == 2
    assert body["target_languages"][0]["language_code"] == "es"
    assert body["target_languages"][0]["tts_enabled"] is True
    assert body["scheduled_at"] is not None


def test_create_session_invalid_transport(client: TestClient, operator: User) -> None:
    resp = client.post(
        "/api/sessions",
        json={"name": "Bad", "audio_transport": "carrier_pigeon"},
        headers=_auth(operator),
    )
    assert resp.status_code == 422


def test_create_session_requires_auth(client: TestClient) -> None:
    resp = client.post("/api/sessions", json={"name": "No Auth"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/sessions  (list)
# ---------------------------------------------------------------------------


def test_list_sessions_empty(client: TestClient, operator: User) -> None:
    resp = client.get("/api/sessions", headers=_auth(operator))
    assert resp.status_code == 200
    body = resp.json()
    assert body["sessions"] == []
    assert body["count"] == 0


def test_list_sessions_returns_own(client: TestClient, operator: User) -> None:
    client.post("/api/sessions", json={"name": "S1"}, headers=_auth(operator))
    client.post("/api/sessions", json={"name": "S2"}, headers=_auth(operator))
    resp = client.get("/api/sessions", headers=_auth(operator))
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


def test_operator_cannot_see_others_sessions(
    client: TestClient, operator: User, operator2: User
) -> None:
    client.post("/api/sessions", json={"name": "Mine"}, headers=_auth(operator))
    resp = client.get("/api/sessions", headers=_auth(operator2))
    assert resp.json()["count"] == 0


def test_admin_sees_all_sessions(
    client: TestClient, operator: User, admin: User
) -> None:
    client.post("/api/sessions", json={"name": "Op Session"}, headers=_auth(operator))
    resp = client.get("/api/sessions", headers=_auth(admin))
    assert resp.json()["count"] == 1


# ---------------------------------------------------------------------------
# GET /api/sessions/{id}  (get detail)
# ---------------------------------------------------------------------------


def test_get_session_by_id(client: TestClient, operator: User) -> None:
    created = client.post(
        "/api/sessions", json={"name": "Detail"}, headers=_auth(operator)
    ).json()
    resp = client.get(f"/api/sessions/{created['id']}", headers=_auth(operator))
    assert resp.status_code == 200
    assert resp.json()["name"] == "Detail"


def test_get_session_not_found(client: TestClient, operator: User) -> None:
    resp = client.get(
        "/api/sessions/00000000-0000-0000-0000-000000000000",
        headers=_auth(operator),
    )
    assert resp.status_code == 404


def test_operator_cannot_get_others_session(
    client: TestClient, operator: User, operator2: User
) -> None:
    created = client.post(
        "/api/sessions", json={"name": "Private"}, headers=_auth(operator)
    ).json()
    resp = client.get(f"/api/sessions/{created['id']}", headers=_auth(operator2))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PATCH /api/sessions/{id}  (update)
# ---------------------------------------------------------------------------


def test_update_session(client: TestClient, operator: User) -> None:
    created = client.post(
        "/api/sessions", json={"name": "Old"}, headers=_auth(operator)
    ).json()
    resp = client.patch(
        f"/api/sessions/{created['id']}",
        json={
            "name": "New",
            "audio_transport": "webrtc",
            "target_languages": [{"language_code": "tl", "tts_enabled": True}],
        },
        headers=_auth(operator),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "New"
    assert body["audio_transport"] == "webrtc"
    assert len(body["target_languages"]) == 1
    assert body["target_languages"][0]["language_code"] == "tl"


def test_update_session_rejects_after_start(client: TestClient, operator: User) -> None:
    created = client.post(
        "/api/sessions", json={"name": "Started"}, headers=_auth(operator)
    ).json()
    client.post(f"/api/sessions/{created['id']}/start", headers=_auth(operator))
    resp = client.patch(
        f"/api/sessions/{created['id']}",
        json={"name": "Nope"},
        headers=_auth(operator),
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /api/sessions/{id}/start
# ---------------------------------------------------------------------------


def test_start_session(client: TestClient, operator: User) -> None:
    created = client.post(
        "/api/sessions", json={"name": "Go"}, headers=_auth(operator)
    ).json()
    resp = client.post(f"/api/sessions/{created['id']}/start", headers=_auth(operator))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["started_at"] is not None


def test_start_already_active(client: TestClient, operator: User) -> None:
    created = client.post(
        "/api/sessions", json={"name": "Already"}, headers=_auth(operator)
    ).json()
    client.post(f"/api/sessions/{created['id']}/start", headers=_auth(operator))
    resp = client.post(f"/api/sessions/{created['id']}/start", headers=_auth(operator))
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /api/sessions/{id}/stop
# ---------------------------------------------------------------------------


def test_stop_session(client: TestClient, operator: User) -> None:
    created = client.post(
        "/api/sessions", json={"name": "End"}, headers=_auth(operator)
    ).json()
    client.post(f"/api/sessions/{created['id']}/start", headers=_auth(operator))
    resp = client.post(f"/api/sessions/{created['id']}/stop", headers=_auth(operator))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ended"
    assert body["ended_at"] is not None


def test_stop_draft_rejected(client: TestClient, operator: User) -> None:
    created = client.post(
        "/api/sessions", json={"name": "Draft"}, headers=_auth(operator)
    ).json()
    resp = client.post(f"/api/sessions/{created['id']}/stop", headers=_auth(operator))
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# GET /api/sessions/join/{code}  (anonymous listener)
# ---------------------------------------------------------------------------


def test_join_by_code_no_auth_required(client: TestClient, operator: User) -> None:
    created = client.post(
        "/api/sessions",
        json={
            "name": "Public",
            "target_languages": [{"language_code": "es", "tts_enabled": False}],
        },
        headers=_auth(operator),
    ).json()
    # No auth header — anonymous.
    resp = client.get(f"/api/sessions/join/{created['join_code']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Public"
    assert body["id"] == created["id"]
    assert len(body["target_languages"]) == 1
    # Listener response must NOT leak operator-internal fields.
    assert "join_slug" not in body
    assert "join_code" not in body
    assert "created_by_user_id" not in body
    assert "join_url" not in body


def test_join_by_slug(client: TestClient, operator: User) -> None:
    created = client.post(
        "/api/sessions", json={"name": "Slug"}, headers=_auth(operator)
    ).json()
    resp = client.get(f"/api/sessions/join/{created['join_slug']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Slug"


def test_join_not_found(client: TestClient) -> None:
    resp = client.get("/api/sessions/join/ZZZZZZ")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Join rate limit
# ---------------------------------------------------------------------------


def test_join_rate_limit(client: TestClient) -> None:
    """Exceed the join rate limit; expect 429 with Retry-After header."""
    from app.core.config import get_settings

    os.environ["JOIN_RATE_LIMIT_MAX_ATTEMPTS"] = "3"
    os.environ["JOIN_RATE_LIMIT_WINDOW_SECONDS"] = "60"
    get_settings.cache_clear()
    reset_join_rate_limiter()

    try:
        for _ in range(3):
            client.get("/api/sessions/join/ZZZZZZ")  # 404 but counts toward limit
        resp = client.get("/api/sessions/join/ZZZZZZ")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
    finally:
        del os.environ["JOIN_RATE_LIMIT_MAX_ATTEMPTS"]
        del os.environ["JOIN_RATE_LIMIT_WINDOW_SECONDS"]
        get_settings.cache_clear()
        reset_join_rate_limiter()


# ---------------------------------------------------------------------------
# stop_session kill-switch (Redis publish)
# ---------------------------------------------------------------------------


def test_stop_session_publishes_kill_switch(client: TestClient, operator: User) -> None:
    """stop_session should publish session_ended to the Redis control channel."""
    created = client.post(
        "/api/sessions", json={"name": "Kill"}, headers=_auth(operator)
    ).json()
    client.post(f"/api/sessions/{created['id']}/start", headers=_auth(operator))

    mock_redis = MagicMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.api.routes.sessions.Redis") as MockRedis:
        MockRedis.from_url.return_value = mock_redis
        resp = client.post(
            f"/api/sessions/{created['id']}/stop", headers=_auth(operator)
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ended"
    # Verify kill-switch was published to the control channel.
    mock_redis.publish.assert_awaited_once()
    channel_arg = mock_redis.publish.call_args[0][0]
    assert channel_arg == f"session:{created['id']}:control"
