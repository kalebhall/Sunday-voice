"""WebSocket authentication helpers.

Browser WebSocket APIs cannot set custom HTTP headers, so the JWT is passed
as a query parameter ``?token=<jwt>``.  This module validates that token and
resolves the operator user, closing the socket with an appropriate code on
failure.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocket, WebSocketState

from app.core.security import TokenError, decode_token
from app.models import User


async def authenticate_ws_operator(
    websocket: WebSocket,
    db: AsyncSession,
) -> User | None:
    """Validate the JWT from the ``token`` query param and return the operator.

    On any auth failure the socket is closed with code **4401** (policy
    violation — unauthorized) and ``None`` is returned so the caller can
    bail out.

    Returns the :class:`User` only if all of the following hold:

    * A valid access-type JWT is present.
    * The referenced user exists and is active.
    * The user's role is ``operator`` or ``admin``.
    """
    token: str | None = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401, reason="missing token")
        return None

    try:
        payload = decode_token(token, expected_type="access")
    except TokenError:
        await websocket.close(code=4401, reason="invalid or expired token")
        return None

    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError, KeyError):
        await websocket.close(code=4401, reason="invalid token subject")
        return None

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()

    if user is None or not user.is_active:
        await websocket.close(code=4401, reason="user not found or disabled")
        return None

    role_name: str | None = getattr(user.role, "name", None) if user.role else None
    if role_name not in ("operator", "admin"):
        await websocket.close(code=4403, reason="insufficient role")
        return None

    return user
