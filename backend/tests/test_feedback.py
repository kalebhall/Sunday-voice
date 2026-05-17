"""Tests for POST /api/feedback — anonymous translation quality signals."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_submit_feedback_success(client: TestClient) -> None:
    resp = client.post(
        "/api/feedback",
        json={
            "segment_id": 1,
            "language_code": "es",
            "session_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 201
    assert resp.json() == {"ok": True}


def test_submit_feedback_no_auth_required(client: TestClient) -> None:
    # Endpoint is anonymous — no Authorization header.
    resp = client.post(
        "/api/feedback",
        json={
            "segment_id": 42,
            "language_code": "to",
            "session_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_submit_feedback_missing_segment_id(client: TestClient) -> None:
    resp = client.post(
        "/api/feedback",
        json={"language_code": "es", "session_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


def test_submit_feedback_segment_id_zero_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/feedback",
        json={"segment_id": 0, "language_code": "es", "session_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


def test_submit_feedback_segment_id_negative_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/feedback",
        json={"segment_id": -1, "language_code": "es", "session_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


def test_submit_feedback_missing_language_code(client: TestClient) -> None:
    resp = client.post(
        "/api/feedback",
        json={"segment_id": 1, "session_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


def test_submit_feedback_invalid_session_id(client: TestClient) -> None:
    resp = client.post(
        "/api/feedback",
        json={"segment_id": 1, "language_code": "es", "session_id": "not-a-uuid"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_submit_feedback_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.routes import feedback as feedback_module
    from app.core.rate_limit import SlidingWindowRateLimiter

    monkeypatch.setattr(
        feedback_module,
        "_feedback_limiter",
        SlidingWindowRateLimiter(max_requests=2, window_seconds=60),
    )

    payload = {"segment_id": 1, "language_code": "es", "session_id": str(uuid.uuid4())}
    assert client.post("/api/feedback", json=payload).status_code == 201
    assert client.post("/api/feedback", json=payload).status_code == 201
    resp = client.post("/api/feedback", json=payload)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
