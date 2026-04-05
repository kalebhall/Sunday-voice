"""Password hashing and JWT token helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

import bcrypt
from jose import JWTError, jwt

from app.core.config import get_settings

TokenType = Literal["access", "refresh"]

# bcrypt only considers the first 72 bytes of the password. Longer passwords
# are accepted but silently truncated (and the ``bcrypt`` package refuses
# them outright on newer versions). We pre-encode and truncate here so the
# caller can submit arbitrary-length passwords without ValueError.
_BCRYPT_MAX_BYTES = 72
_BCRYPT_COST = 12


def _encode(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


class TokenError(Exception):
    """Raised when a JWT cannot be decoded or fails validation."""


def hash_password(password: str) -> str:
    hashed = bcrypt.hashpw(_encode(password), bcrypt.gensalt(rounds=_BCRYPT_COST))
    return hashed.decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_encode(password), hashed.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False


def _create_token(
    *,
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": uuid4().hex,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(
    *, user_id: int, role: str, extra_claims: dict[str, Any] | None = None
) -> str:
    settings = get_settings()
    claims: dict[str, Any] = {"role": role}
    if extra_claims:
        claims.update(extra_claims)
    return _create_token(
        subject=str(user_id),
        token_type="access",
        expires_delta=timedelta(minutes=settings.jwt_access_token_ttl_minutes),
        extra_claims=claims,
    )


def create_refresh_token(*, user_id: int) -> str:
    settings = get_settings()
    return _create_token(
        subject=str(user_id),
        token_type="refresh",
        expires_delta=timedelta(days=settings.jwt_refresh_token_ttl_days),
    )


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise TokenError(f"invalid token: {exc}") from exc
    if payload.get("type") != expected_type:
        raise TokenError(f"expected token type {expected_type!r}, got {payload.get('type')!r}")
    if "sub" not in payload:
        raise TokenError("token missing subject")
    return payload
