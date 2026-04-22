"""End-to-end tests for local auth (login, refresh, /me, require_role)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser, require_role, reset_login_rate_limiter
from app.core.config import Settings, get_settings
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.session import get_session
from app.main import app
from app.models import Role, User


async def _make_user(
    maker: async_sessionmaker[AsyncSession],
    *,
    email: str,
    password: str,
    role_name: str,
    is_active: bool = True,
    display_name: str = "Test User",
) -> User:
    async with maker() as session:
        role = (
            await session.execute(select(Role).where(Role.name == role_name))
        ).scalar_one()
        user = User(
            email=email,
            display_name=display_name,
            hashed_password=hash_password(password),
            role_id=role.id,
            is_active=is_active,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest_asyncio.fixture
async def admin_user(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> User:
    return await _make_user(
        db_sessionmaker,
        email="admin@example.com",
        password="correct-horse-battery",
        role_name="admin",
    )


@pytest_asyncio.fixture
async def operator_user(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> User:
    return await _make_user(
        db_sessionmaker,
        email="op@example.com",
        password="another-strong-pass",
        role_name="operator",
    )


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #


def test_password_hash_roundtrip() -> None:
    h = hash_password("hunter2")
    assert h != "hunter2"
    assert h.startswith("$2")  # bcrypt
    assert verify_password("hunter2", h)
    assert not verify_password("hunter3", h)


def test_verify_password_rejects_malformed_hash() -> None:
    assert not verify_password("x", "not-a-hash")


def test_hash_password_generates_distinct_salts() -> None:
    assert hash_password("same") != hash_password("same")


# --------------------------------------------------------------------------- #
# JWT tokens
# --------------------------------------------------------------------------- #


def test_access_token_roundtrip() -> None:
    tok = create_access_token(user_id=42, role="admin")
    claims = decode_token(tok, expected_type="access")
    assert claims["sub"] == "42"
    assert claims["role"] == "admin"
    assert claims["type"] == "access"
    assert "exp" in claims and "iat" in claims and "jti" in claims


def test_refresh_token_roundtrip() -> None:
    tok = create_refresh_token(user_id=7)
    claims = decode_token(tok, expected_type="refresh")
    assert claims["sub"] == "7"
    assert claims["type"] == "refresh"


def test_decode_rejects_wrong_type() -> None:
    access = create_access_token(user_id=1, role="operator")
    with pytest.raises(TokenError):
        decode_token(access, expected_type="refresh")


def test_decode_rejects_garbage() -> None:
    with pytest.raises(TokenError):
        decode_token("not.a.jwt", expected_type="access")


def test_decode_rejects_wrong_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    tok = create_access_token(user_id=1, role="admin")
    monkeypatch.setenv("SECRET_KEY", "a-totally-different-secret-value-here!!")
    get_settings.cache_clear()
    with pytest.raises(TokenError):
        decode_token(tok, expected_type="access")


# --------------------------------------------------------------------------- #
# /api/auth/login
# --------------------------------------------------------------------------- #


def test_login_success(client: TestClient, admin_user: User) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["access_token"]
    # Refresh token is delivered as an HttpOnly cookie, not in the body.
    assert "refresh_token" not in body
    settings = get_settings()
    cookie = resp.cookies.get(settings.refresh_cookie_name)
    assert cookie, "refresh cookie should be set on login"
    refresh_claims = decode_token(cookie, expected_type="refresh")
    assert refresh_claims["sub"] == str(admin_user.id)
    # Token should carry the role claim.
    claims = decode_token(body["access_token"], expected_type="access")
    assert claims["role"] == "admin"
    assert claims["sub"] == str(admin_user.id)


def _cookie_attrs(resp, name: str) -> dict[str, str]:
    """Parse the Set-Cookie header for *name* into a flag dict."""
    for header in resp.headers.get_list("set-cookie"):
        if header.split("=", 1)[0].strip().lower() == name.lower():
            parts = [p.strip() for p in header.split(";")]
            attrs: dict[str, str] = {}
            for part in parts[1:]:
                if "=" in part:
                    k, v = part.split("=", 1)
                    attrs[k.strip().lower()] = v.strip()
                else:
                    attrs[part.lower()] = ""
            return attrs
    return {}


def test_login_sets_secure_cookie_flags(
    client: TestClient, admin_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force a prod-like environment so the Secure flag is asserted.
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    reset_login_rate_limiter()
    resp = client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 200, resp.text
    settings = get_settings()
    attrs = _cookie_attrs(resp, settings.refresh_cookie_name)
    assert "httponly" in attrs
    assert "secure" in attrs
    assert attrs.get("samesite", "").lower() == "strict"
    assert attrs.get("path") == settings.refresh_cookie_path


def test_login_cookie_not_secure_in_development(
    client: TestClient, admin_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    reset_login_rate_limiter()
    resp = client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 200, resp.text
    attrs = _cookie_attrs(resp, get_settings().refresh_cookie_name)
    # HttpOnly and SameSite must still be set; Secure is relaxed.
    assert "httponly" in attrs
    assert "secure" not in attrs


def test_login_wrong_password(client: TestClient, admin_user: User) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "nope"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid credentials"


def test_login_unknown_email_uses_same_error(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"email": "ghost@example.com", "password": "whatever"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid credentials"


@pytest_asyncio.fixture
async def disabled_user(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> User:
    return await _make_user(
        db_sessionmaker,
        email="dead@example.com",
        password="pw-for-dead-user",
        role_name="operator",
        is_active=False,
    )


def test_login_disabled_user_rejected(
    client: TestClient, disabled_user: User
) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"email": "dead@example.com", "password": "pw-for-dead-user"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid credentials"


def test_login_rate_limit_trips_after_n_attempts(
    client: TestClient, admin_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOGIN_RATE_LIMIT_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    reset_login_rate_limiter()

    body = {"email": "admin@example.com", "password": "wrong"}
    # First 3 attempts return 401.
    for _ in range(3):
        r = client.post("/api/auth/login", json=body)
        assert r.status_code == 401
    # Fourth is throttled.
    r = client.post("/api/auth/login", json=body)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    # Even a correct password is blocked while the window holds.
    r = client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "correct-horse-battery"},
    )
    assert r.status_code == 429


def test_login_rate_limit_is_per_email_and_client(
    client: TestClient, admin_user: User, operator_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGIN_RATE_LIMIT_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    reset_login_rate_limiter()

    # Burn the admin bucket.
    for _ in range(2):
        assert (
            client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "nope"},
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "nope"},
        ).status_code
        == 429
    )
    # Different email on the same client is still allowed.
    r = client.post(
        "/api/auth/login",
        json={"email": "op@example.com", "password": "another-strong-pass"},
    )
    assert r.status_code == 200


def test_login_validation_error_on_missing_fields(client: TestClient) -> None:
    resp = client.post("/api/auth/login", json={"email": "a@b.co"})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# /api/auth/refresh
# --------------------------------------------------------------------------- #


def _settings() -> Settings:
    return get_settings()


def test_refresh_success(client: TestClient, operator_user: User) -> None:
    login = client.post(
        "/api/auth/login",
        json={"email": "op@example.com", "password": "another-strong-pass"},
    )
    assert login.status_code == 200, login.text
    # TestClient carries the cookie over automatically; /refresh needs no body.
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert "refresh_token" not in body
    # Rotation: a fresh refresh cookie is issued on every successful refresh.
    rotated = resp.cookies.get(_settings().refresh_cookie_name)
    assert rotated, "refresh should rotate the cookie"
    claims = decode_token(body["access_token"], expected_type="access")
    assert claims["role"] == "operator"
    assert claims["sub"] == str(operator_user.id)


def test_refresh_without_cookie_is_rejected(client: TestClient) -> None:
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 401


def test_refresh_rejects_access_token(client: TestClient, admin_user: User) -> None:
    access = create_access_token(user_id=admin_user.id, role="admin")
    resp = client.post(
        "/api/auth/refresh",
        cookies={_settings().refresh_cookie_name: access},
    )
    assert resp.status_code == 401


def test_refresh_rejects_unknown_user(client: TestClient) -> None:
    tok = create_refresh_token(user_id=999_999)
    resp = client.post(
        "/api/auth/refresh",
        cookies={_settings().refresh_cookie_name: tok},
    )
    assert resp.status_code == 401


def test_refresh_rejects_garbage(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/refresh",
        cookies={_settings().refresh_cookie_name: "abc"},
    )
    assert resp.status_code == 401
    # Bad cookie should be instructed to clear so the browser stops resending.
    attrs = _cookie_attrs(resp, _settings().refresh_cookie_name)
    assert attrs, "response should include a Set-Cookie to clear the bad cookie"


# --------------------------------------------------------------------------- #
# /api/auth/logout
# --------------------------------------------------------------------------- #


def test_logout_clears_cookie(client: TestClient, admin_user: User) -> None:
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "correct-horse-battery"},
    )
    assert login.status_code == 200
    name = _settings().refresh_cookie_name
    assert login.cookies.get(name)

    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204
    # After logout the browser no longer holds a valid refresh cookie, so
    # /refresh should fail.
    client.cookies.delete(name)
    assert client.post("/api/auth/refresh").status_code == 401


# --------------------------------------------------------------------------- #
# /api/auth/me
# --------------------------------------------------------------------------- #


def test_me_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_me_returns_current_user(client: TestClient, admin_user: User) -> None:
    tok = create_access_token(user_id=admin_user.id, role="admin")
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "admin@example.com"
    assert body["role"] == "admin"
    assert body["is_active"] is True
    assert body["id"] == admin_user.id


def test_me_rejects_refresh_token_in_bearer(
    client: TestClient, admin_user: User
) -> None:
    tok = create_refresh_token(user_id=admin_user.id)
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 401


def test_me_rejects_tampered_token(client: TestClient, admin_user: User) -> None:
    tok = create_access_token(user_id=admin_user.id, role="admin")
    tampered = tok[:-2] + ("AA" if tok[-2:] != "AA" else "BB")
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tampered}"})
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# require_role()
# --------------------------------------------------------------------------- #


@pytest.fixture
def role_guarded_client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> Iterator[TestClient]:
    """Mount a handful of routes guarded by require_role() on the live app."""
    router_app = app  # reuse the main app so middleware / overrides apply

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    router_app.dependency_overrides[get_session] = _override_get_session

    # Register once; idempotent for the test process.
    if not any(
        getattr(r, "path", "") == "/__test_admin_only" for r in router_app.routes
    ):

        @router_app.get("/__test_admin_only")
        async def _admin(user: User = Depends(require_role("admin"))) -> dict[str, str]:
            return {"email": user.email}

        @router_app.get("/__test_any")
        async def _any(
            user: User = Depends(require_role("admin", "operator")),
        ) -> dict[str, str]:
            return {"email": user.email}

    try:
        with TestClient(router_app) as tc:
            yield tc
    finally:
        router_app.dependency_overrides.pop(get_session, None)


def test_require_role_accepts_exact_match(
    role_guarded_client: TestClient, admin_user: User
) -> None:
    tok = create_access_token(user_id=admin_user.id, role="admin")
    r = role_guarded_client.get(
        "/__test_admin_only", headers={"Authorization": f"Bearer {tok}"}
    )
    assert r.status_code == 200
    assert r.json()["email"] == "admin@example.com"


def test_require_role_forbids_other_role(
    role_guarded_client: TestClient, operator_user: User
) -> None:
    tok = create_access_token(user_id=operator_user.id, role="operator")
    r = role_guarded_client.get(
        "/__test_admin_only", headers={"Authorization": f"Bearer {tok}"}
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "insufficient role"


def test_require_role_allows_any_listed_role(
    role_guarded_client: TestClient, operator_user: User, admin_user: User
) -> None:
    for u in (operator_user, admin_user):
        tok = create_access_token(user_id=u.id, role=u.role.name)
        r = role_guarded_client.get(
            "/__test_any", headers={"Authorization": f"Bearer {tok}"}
        )
        assert r.status_code == 200


def test_require_role_requires_at_least_one_name() -> None:
    with pytest.raises(ValueError):
        require_role()


# --------------------------------------------------------------------------- #
# Rate limiter unit tests
# --------------------------------------------------------------------------- #


def test_sliding_window_basic() -> None:
    rl = SlidingWindowRateLimiter(max_requests=2, window_seconds=60.0)
    assert rl.check("k").allowed
    assert rl.check("k").allowed
    r = rl.check("k")
    assert not r.allowed
    assert r.retry_after_seconds >= 0
    # Different key is independent.
    assert rl.check("other").allowed


def test_sliding_window_reset() -> None:
    rl = SlidingWindowRateLimiter(max_requests=1, window_seconds=60.0)
    assert rl.check("k").allowed
    assert not rl.check("k").allowed
    rl.reset("k")
    assert rl.check("k").allowed


def test_sliding_window_rejects_invalid_config() -> None:
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_requests=0, window_seconds=1.0)
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_requests=1, window_seconds=0)
