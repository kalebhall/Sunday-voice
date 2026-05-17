"""Tests for admin-only routes: users, roles, usage, audit-logs, retention, budget."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.core.security import create_access_token, hash_password
from app.models import Role, User


async def _make_user(
    maker: async_sessionmaker[AsyncSession],
    *,
    email: str,
    role_name: str,
    password: str = "securepass123",
) -> User:
    async with maker() as session:
        role = (
            await session.execute(select(Role).where(Role.name == role_name))
        ).scalar_one()
        user = User(
            email=email,
            display_name=f"Test {role_name.capitalize()}",
            hashed_password=hash_password(password),
            role_id=role.id,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest_asyncio.fixture
async def admin_user(db_sessionmaker: async_sessionmaker[AsyncSession]) -> User:
    return await _make_user(db_sessionmaker, email="admin@test.com", role_name="admin")


@pytest_asyncio.fixture
async def operator_user(db_sessionmaker: async_sessionmaker[AsyncSession]) -> User:
    return await _make_user(db_sessionmaker, email="op@test.com", role_name="operator")


def _auth(user: User) -> dict[str, str]:
    tok = create_access_token(user_id=user.id, role=user.role.name)
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
# Authorization guard
# ---------------------------------------------------------------------------


def test_admin_endpoints_reject_operator(client: TestClient, operator_user: User) -> None:
    hdrs = _auth(operator_user)
    for url in (
        "/api/admin/users",
        "/api/admin/roles",
        "/api/admin/usage",
        "/api/admin/audit-logs",
        "/api/admin/retention",
        "/api/admin/budget",
    ):
        assert client.get(url, headers=hdrs).status_code == 403, url


def test_admin_endpoints_reject_unauthenticated(client: TestClient) -> None:
    assert client.get("/api/admin/users").status_code == 401


# ---------------------------------------------------------------------------
# GET /api/admin/users
# ---------------------------------------------------------------------------


def test_list_users(client: TestClient, admin_user: User) -> None:
    resp = client.get("/api/admin/users", headers=_auth(admin_user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    emails = [u["email"] for u in body["users"]]
    assert "admin@test.com" in emails


def test_list_users_pagination(client: TestClient, admin_user: User) -> None:
    resp = client.get("/api/admin/users?page=1&page_size=1", headers=_auth(admin_user))
    assert resp.status_code == 200
    assert len(resp.json()["users"]) <= 1


# ---------------------------------------------------------------------------
# POST /api/admin/users
# ---------------------------------------------------------------------------


def _operator_role_id(client: TestClient, admin_user: User) -> int:
    roles = client.get("/api/admin/roles", headers=_auth(admin_user)).json()["roles"]
    return next(r["id"] for r in roles if r["name"] == "operator")


def test_create_user(client: TestClient, admin_user: User) -> None:
    role_id = _operator_role_id(client, admin_user)
    resp = client.post(
        "/api/admin/users",
        headers=_auth(admin_user),
        json={
            "email": "newop@test.com",
            "password": "securepass123",
            "display_name": "New Operator",
            "role_id": role_id,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "newop@test.com"
    assert body["role"]["name"] == "operator"
    assert body["is_active"] is True


def test_create_user_duplicate_email(client: TestClient, admin_user: User) -> None:
    role_id = _operator_role_id(client, admin_user)
    payload = {
        "email": "dup@test.com",
        "password": "securepass123",
        "display_name": "Dup",
        "role_id": role_id,
    }
    client.post("/api/admin/users", headers=_auth(admin_user), json=payload)
    resp = client.post("/api/admin/users", headers=_auth(admin_user), json=payload)
    assert resp.status_code == 409
    assert "already registered" in resp.json()["detail"]


def test_create_user_unknown_role(client: TestClient, admin_user: User) -> None:
    resp = client.post(
        "/api/admin/users",
        headers=_auth(admin_user),
        json={
            "email": "nobody@test.com",
            "password": "securepass123",
            "display_name": "Nobody",
            "role_id": 9999,
        },
    )
    assert resp.status_code == 422


def test_create_user_short_password(client: TestClient, admin_user: User) -> None:
    role_id = _operator_role_id(client, admin_user)
    resp = client.post(
        "/api/admin/users",
        headers=_auth(admin_user),
        json={
            "email": "short@test.com",
            "password": "tiny",
            "display_name": "Short",
            "role_id": role_id,
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/admin/users/{id}
# ---------------------------------------------------------------------------


def test_get_user_by_id(
    client: TestClient, admin_user: User, operator_user: User
) -> None:
    resp = client.get(f"/api/admin/users/{operator_user.id}", headers=_auth(admin_user))
    assert resp.status_code == 200
    assert resp.json()["email"] == "op@test.com"


def test_get_user_not_found(client: TestClient, admin_user: User) -> None:
    resp = client.get("/api/admin/users/999999", headers=_auth(admin_user))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/admin/users/{id}
# ---------------------------------------------------------------------------


def test_update_user_display_name(
    client: TestClient, admin_user: User, operator_user: User
) -> None:
    resp = client.patch(
        f"/api/admin/users/{operator_user.id}",
        headers=_auth(admin_user),
        json={"display_name": "Renamed Operator"},
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Renamed Operator"


def test_deactivate_user_via_patch(
    client: TestClient, admin_user: User, operator_user: User
) -> None:
    resp = client.patch(
        f"/api/admin/users/{operator_user.id}",
        headers=_auth(admin_user),
        json={"is_active": False},
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


def test_cannot_demote_sole_admin(client: TestClient, admin_user: User) -> None:
    role_id = _operator_role_id(client, admin_user)
    resp = client.patch(
        f"/api/admin/users/{admin_user.id}",
        headers=_auth(admin_user),
        json={"role_id": role_id},
    )
    assert resp.status_code == 409
    assert "admin" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# DELETE /api/admin/users/{id}
# ---------------------------------------------------------------------------


def test_deactivate_user(
    client: TestClient, admin_user: User, operator_user: User
) -> None:
    resp = client.delete(f"/api/admin/users/{operator_user.id}", headers=_auth(admin_user))
    assert resp.status_code == 204
    detail = client.get(
        f"/api/admin/users/{operator_user.id}", headers=_auth(admin_user)
    ).json()
    assert detail["is_active"] is False


def test_cannot_deactivate_self(client: TestClient, admin_user: User) -> None:
    resp = client.delete(f"/api/admin/users/{admin_user.id}", headers=_auth(admin_user))
    assert resp.status_code == 409
    assert "own account" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/admin/roles
# ---------------------------------------------------------------------------


def test_list_roles(client: TestClient, admin_user: User) -> None:
    resp = client.get("/api/admin/roles", headers=_auth(admin_user))
    assert resp.status_code == 200
    names = {r["name"] for r in resp.json()["roles"]}
    assert {"admin", "operator"} <= names


# ---------------------------------------------------------------------------
# GET /api/admin/usage
# ---------------------------------------------------------------------------


def test_usage_empty_period(client: TestClient, admin_user: User) -> None:
    resp = client.get("/api/admin/usage", headers=_auth(admin_user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert float(body["total_cost_usd"]) == 0.0
    assert body["alert_triggered"] is False


def test_usage_explicit_period(client: TestClient, admin_user: User) -> None:
    resp = client.get("/api/admin/usage?period=2026-05", headers=_auth(admin_user))
    assert resp.status_code == 200
    assert resp.json()["period"] == "2026-05"


def test_usage_invalid_period_format(client: TestClient, admin_user: User) -> None:
    resp = client.get("/api/admin/usage?period=2026-5", headers=_auth(admin_user))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/admin/audit-logs
# ---------------------------------------------------------------------------


def test_audit_logs_empty(client: TestClient, admin_user: User) -> None:
    resp = client.get("/api/admin/audit-logs", headers=_auth(admin_user))
    assert resp.status_code == 200
    body = resp.json()
    assert "logs" in body
    assert "total" in body
    assert body["page"] == 1
    assert body["page_size"] == 50


def test_audit_logs_populated_after_session_create(
    client: TestClient, admin_user: User, operator_user: User
) -> None:
    # Creating a session writes an audit_log row via write_audit_log.
    client.post(
        "/api/sessions",
        json={"name": "Audit Trail Session"},
        headers=_auth(operator_user),
    )
    resp = client.get(
        f"/api/admin/audit-logs?actor_user_id={operator_user.id}",
        headers=_auth(admin_user),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    actions = [log["action"] for log in body["logs"]]
    assert "session.create" in actions


def test_audit_logs_filter_by_action(
    client: TestClient, admin_user: User, operator_user: User
) -> None:
    client.post("/api/sessions", json={"name": "S1"}, headers=_auth(operator_user))
    resp = client.get(
        "/api/admin/audit-logs?action=session.create",
        headers=_auth(admin_user),
    )
    assert resp.status_code == 200
    assert all(log["action"] == "session.create" for log in resp.json()["logs"])


# ---------------------------------------------------------------------------
# GET /api/admin/retention
# ---------------------------------------------------------------------------


def test_retention_status(client: TestClient, admin_user: User) -> None:
    resp = client.get("/api/admin/retention", headers=_auth(admin_user))
    assert resp.status_code == 200
    body = resp.json()
    assert "retention_hours" in body
    assert "cleanup_enabled" in body
    assert body["last_cleanup"] is None


# ---------------------------------------------------------------------------
# GET / PATCH /api/admin/budget
# ---------------------------------------------------------------------------


def test_budget_defaults_from_env(client: TestClient, admin_user: User) -> None:
    resp = client.get("/api/admin/budget", headers=_auth(admin_user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["monthly_budget_usd"] > 0
    assert 0 < body["alert_threshold"] <= 1.0
    assert body["source"] == "env"


def test_update_budget(client: TestClient, admin_user: User) -> None:
    resp = client.patch(
        "/api/admin/budget",
        headers=_auth(admin_user),
        json={"monthly_budget_usd": 25.0, "alert_threshold": 0.9},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["monthly_budget_usd"] == 25.0
    assert body["alert_threshold"] == 0.9
    assert body["source"] == "db"


def test_update_budget_persists(client: TestClient, admin_user: User) -> None:
    client.patch(
        "/api/admin/budget",
        headers=_auth(admin_user),
        json={"monthly_budget_usd": 50.0, "alert_threshold": 0.75},
    )
    resp = client.get("/api/admin/budget", headers=_auth(admin_user))
    assert resp.json()["monthly_budget_usd"] == 50.0
    assert resp.json()["source"] == "db"


def test_update_budget_threshold_above_one_rejected(
    client: TestClient, admin_user: User
) -> None:
    resp = client.patch(
        "/api/admin/budget",
        headers=_auth(admin_user),
        json={"monthly_budget_usd": 10.0, "alert_threshold": 1.5},
    )
    assert resp.status_code == 422


def test_update_budget_negative_amount_rejected(
    client: TestClient, admin_user: User
) -> None:
    resp = client.patch(
        "/api/admin/budget",
        headers=_auth(admin_user),
        json={"monthly_budget_usd": -5.0, "alert_threshold": 0.8},
    )
    assert resp.status_code == 422
