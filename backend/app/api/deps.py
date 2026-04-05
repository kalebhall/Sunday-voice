"""Shared FastAPI dependencies (auth, DB session, rate limiting)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.security import TokenError, decode_token
from app.db.session import get_session
from app.models import User

# Single shared bearer-token extractor. auto_error=False lets us return a
# consistent 401 envelope below rather than Fast API's default 403.
_bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_session)]

_login_rate_limiter: SlidingWindowRateLimiter | None = None


def get_login_rate_limiter() -> SlidingWindowRateLimiter:
    """Return the process-wide login rate limiter, building it lazily."""
    global _login_rate_limiter
    if _login_rate_limiter is None:
        settings = get_settings()
        _login_rate_limiter = SlidingWindowRateLimiter(
            max_requests=settings.login_rate_limit_max_attempts,
            window_seconds=settings.login_rate_limit_window_seconds,
        )
    return _login_rate_limiter


def reset_login_rate_limiter() -> None:
    """Discard the cached limiter; used in tests to pick up config changes."""
    global _login_rate_limiter
    _login_rate_limiter = None


def client_identifier(request: Request) -> str:
    """Best-effort client identifier for rate limiting.

    Uses X-Forwarded-For (first entry) when present — the app is designed to
    sit behind Caddy/Nginx per docs/deployment.md — falling back to the peer
    address. This is intentionally coarse; login endpoint also keys on email
    so a single IP churning through accounts is still throttled.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client is not None:
        return request.client.host
    return "unknown"


async def get_current_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token subject",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user not found or disabled",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Stash on request for downstream handlers / audit logging.
    request.state.user_id = user.id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*allowed: str) -> Callable[[User], User]:
    """Dependency factory that enforces the current user holds one of ``allowed``.

    Usage::

        @router.get("/admin-only", dependencies=[Depends(require_role("admin"))])
        async def handler(...): ...

    Or to read the user::

        async def handler(user: User = Depends(require_role("admin", "operator"))):
            ...
    """
    if not allowed:
        raise ValueError("require_role() needs at least one role name")
    allowed_set = frozenset(allowed)

    def _dep(user: CurrentUser) -> User:
        if user.role.name not in allowed_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient role",
            )
        return user

    _dep.__name__ = f"require_role_{'_'.join(sorted(allowed_set))}"
    return _dep


def require_any_role(allowed: Iterable[str]) -> Callable[[User], User]:
    """Convenience wrapper for when role names are assembled dynamically."""
    return require_role(*allowed)
