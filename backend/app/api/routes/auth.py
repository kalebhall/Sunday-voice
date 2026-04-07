"""Authentication endpoints: login, refresh, and current user."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    DbSession,
    client_identifier,
    get_login_rate_limiter,
)
from app.core.audit import write_audit_log
from app.core.config import get_settings
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.models import User
from app.schemas.auth import LoginRequest, MeResponse, RefreshRequest, TokenResponse

router = APIRouter()


def _token_response(user: User) -> TokenResponse:
    settings = get_settings()
    return TokenResponse(
        access_token=create_access_token(user_id=user.id, role=user.role.name),
        refresh_token=create_refresh_token(user_id=user.id),
        expires_in=settings.jwt_access_token_ttl_minutes * 60,
    )


async def _load_user_with_role(db: AsyncSession, user_id: int) -> User | None:
    return (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    db: DbSession,
    limiter: Annotated[SlidingWindowRateLimiter, Depends(get_login_rate_limiter)],
) -> TokenResponse:
    # Rate limit keyed by (client, email) so one attacker cycling emails and
    # one attacker focused on one email are both throttled.
    ident = client_identifier(request)
    key = f"login:{ident}:{payload.email.lower()}"
    result = limiter.check(key)
    if not result.allowed:
        retry_after = max(1, int(result.retry_after_seconds) + 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )

    user = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()

    # Constant-ish error path: same message whether the user exists, is
    # disabled, or the password is wrong. Avoids email enumeration.
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid credentials",
    )
    if user is None or not user.is_active:
        # Still run a verify call against a dummy hash so timing doesn't leak
        # account existence. Cheap relative to a real bcrypt check anyway.
        verify_password(payload.password, _DUMMY_HASH)
        write_audit_log(
            db,
            action="auth.login.failed",
            ip_address=ident,
            user_agent=request.headers.get("user-agent"),
            details={"reason": "user_not_found_or_inactive", "email": payload.email},
        )
        await db.commit()
        raise invalid
    if not verify_password(payload.password, user.hashed_password):
        write_audit_log(
            db,
            action="auth.login.failed",
            actor_user_id=user.id,
            ip_address=ident,
            user_agent=request.headers.get("user-agent"),
            details={"reason": "invalid_password"},
        )
        await db.commit()
        raise invalid

    user.last_login_at = datetime.now(timezone.utc)
    write_audit_log(
        db,
        action="auth.login",
        actor_user_id=user.id,
        ip_address=ident,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    await db.refresh(user)

    return _token_response(user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenResponse:
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token subject",
        ) from exc

    user = await _load_user_with_role(db, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user not found or disabled",
        )
    write_audit_log(db, action="auth.token_refresh", actor_user_id=user.id)
    await db.commit()
    return _token_response(user)


@router.get("/me", response_model=MeResponse)
async def me(current: CurrentUser) -> MeResponse:
    return MeResponse(
        id=current.id,
        email=current.email,
        display_name=current.display_name,
        role=current.role.name,
        is_active=current.is_active,
    )


# A precomputed bcrypt hash of a random string. Used only to equalise timing
# on the "user not found" branch of /login.
_DUMMY_HASH = "$2b$12$CwTycUXWue0Thq9StjUM0uJ8QZ6r1d2b9mFMQ9C9mW9MC9Y1B9M9W"
