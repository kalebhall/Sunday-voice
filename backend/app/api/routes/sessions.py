"""Session management endpoints for operators and anonymous listeners."""

from __future__ import annotations

import json
import logging
import secrets
import string
import uuid
from datetime import UTC, datetime
from typing import Annotated

from redis.asyncio import Redis

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession, client_identifier, get_join_rate_limiter, require_role
from app.core.audit import write_audit_log
from app.core.config import get_settings
from app.core.metrics import active_sessions as active_sessions_gauge
from app.core.rate_limit import SlidingWindowRateLimiter
from app.models import AudioTransport, Session, SessionLanguage, SessionStatus, User
from app.schemas.session import (
    LanguageOut,
    ListenerSessionOut,
    SessionCreate,
    SessionListOut,
    SessionOut,
    SessionUpdate,
)

router = APIRouter()

# Module-level dependency to satisfy B008 (no function calls in defaults).
_require_operator = require_role("admin", "operator")
OperatorUser = Annotated[User, Depends(_require_operator)]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JOIN_CODE_ALPHABET = string.ascii_uppercase + string.digits
_JOIN_CODE_LENGTH = 6
_JOIN_SLUG_BYTES = 16  # 22-char URL-safe base64


def _generate_join_code() -> str:
    """Short human-enterable code (e.g. ``A3X9K2``)."""
    return "".join(secrets.choice(_JOIN_CODE_ALPHABET) for _ in range(_JOIN_CODE_LENGTH))


def _generate_join_slug() -> str:
    """Non-guessable URL-safe slug for QR / link sharing."""
    return secrets.token_urlsafe(_JOIN_SLUG_BYTES)


def _validate_audio_transport(value: str) -> AudioTransport:
    try:
        return AudioTransport(value)
    except ValueError as exc:
        allowed = ", ".join(t.value for t in AudioTransport)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid audio_transport; allowed: {allowed}",
        ) from exc


def _session_out(session: Session) -> SessionOut:
    settings = get_settings()
    base = settings.app_base_url.rstrip("/")
    return SessionOut(
        id=session.id,
        name=session.name,
        join_slug=session.join_slug,
        join_code=session.join_code,
        join_url=f"{base}/join/{session.join_slug}",
        source_language=session.source_language,
        status=session.status.value,
        audio_transport=session.audio_transport.value,
        target_languages=[
            LanguageOut(language_code=lang.language_code, tts_enabled=lang.tts_enabled)
            for lang in session.languages
        ],
        scheduled_at=session.scheduled_at,
        started_at=session.started_at,
        ended_at=session.ended_at,
        created_at=session.created_at,
        created_by_user_id=session.created_by_user_id,
    )


async def _get_operator_session(
    session_id: uuid.UUID,
    db: DbSession,
    user: User,
) -> Session:
    """Load a session by ID, ensuring the current operator owns it (admins bypass)."""
    stmt = (
        select(Session)
        .options(selectinload(Session.languages))
        .where(Session.id == session_id)
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    if user.role.name != "admin" and session.created_by_user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not your session")
    return session


# ---------------------------------------------------------------------------
# Operator endpoints (require admin or operator role)
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    payload: SessionCreate,
    db: DbSession,
    user: OperatorUser,
) -> SessionOut:
    """Create a new session (ad-hoc or scheduled)."""
    transport = _validate_audio_transport(payload.audio_transport)

    session = Session(
        id=uuid.uuid4(),
        name=payload.name,
        join_slug=_generate_join_slug(),
        join_code=_generate_join_code(),
        source_language=payload.source_language,
        status=SessionStatus.DRAFT,
        audio_transport=transport,
        scheduled_at=payload.scheduled_at,
        created_by_user_id=user.id,
    )
    for lang in payload.target_languages:
        session.languages.append(
            SessionLanguage(
                language_code=lang.language_code,
                tts_enabled=lang.tts_enabled,
            )
        )
    db.add(session)
    write_audit_log(
        db,
        action="session.create",
        actor_user_id=user.id,
        target_type="session",
        target_id=str(session.id),
        details={"name": session.name, "source_language": session.source_language},
    )
    await db.commit()
    await db.refresh(session, attribute_names=["languages"])
    return _session_out(session)


@router.get("", response_model=SessionListOut)
async def list_sessions(
    db: DbSession,
    user: OperatorUser,
) -> SessionListOut:
    """List sessions. Operators see only their own; admins see all."""
    stmt = (
        select(Session)
        .options(selectinload(Session.languages))
        .order_by(Session.created_at.desc())
    )
    if user.role.name != "admin":
        stmt = stmt.where(Session.created_by_user_id == user.id)
    rows = (await db.execute(stmt)).scalars().all()
    return SessionListOut(
        sessions=[_session_out(s) for s in rows],
        count=len(rows),
    )


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: uuid.UUID,
    db: DbSession,
    user: OperatorUser,
) -> SessionOut:
    """Get a single session by ID."""
    session = await _get_operator_session(session_id, db, user)
    return _session_out(session)


@router.patch("/{session_id}", response_model=SessionOut)
async def update_session(
    session_id: uuid.UUID,
    payload: SessionUpdate,
    db: DbSession,
    user: OperatorUser,
) -> SessionOut:
    """Update session configuration. Only allowed while status is draft."""
    session = await _get_operator_session(session_id, db, user)
    if session.status != SessionStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="can only update sessions in draft status",
        )

    if payload.name is not None:
        session.name = payload.name
    if payload.source_language is not None:
        session.source_language = payload.source_language
    if payload.audio_transport is not None:
        session.audio_transport = _validate_audio_transport(payload.audio_transport)

    if payload.target_languages is not None:
        # Replace all target languages.
        session.languages.clear()
        for lang in payload.target_languages:
            session.languages.append(
                SessionLanguage(
                    language_code=lang.language_code,
                    tts_enabled=lang.tts_enabled,
                )
            )

    await db.commit()
    await db.refresh(session, attribute_names=["languages"])
    return _session_out(session)


@router.post("/{session_id}/start", response_model=SessionOut)
async def start_session(
    session_id: uuid.UUID,
    db: DbSession,
    user: OperatorUser,
) -> SessionOut:
    """Transition a session from draft to active."""
    session = await _get_operator_session(session_id, db, user)
    if session.status != SessionStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot start session in {session.status.value} status",
        )
    session.status = SessionStatus.ACTIVE
    session.started_at = datetime.now(tz=UTC)
    write_audit_log(
        db,
        action="session.start",
        actor_user_id=user.id,
        target_type="session",
        target_id=str(session.id),
    )
    await db.commit()
    active_sessions_gauge.inc()
    await db.refresh(session, attribute_names=["languages"])
    return _session_out(session)


@router.post("/{session_id}/stop", response_model=SessionOut)
async def stop_session(
    session_id: uuid.UUID,
    db: DbSession,
    user: OperatorUser,
) -> SessionOut:
    """Transition a session from active to ended.

    After committing the status change a kill-switch message is published to
    the session's Redis control channel so all active listener WebSocket
    connections are immediately closed.
    """
    session = await _get_operator_session(session_id, db, user)
    if session.status != SessionStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot stop session in {session.status.value} status",
        )
    session.status = SessionStatus.ENDED
    session.ended_at = datetime.now(tz=UTC)
    write_audit_log(
        db,
        action="session.stop",
        actor_user_id=user.id,
        target_type="session",
        target_id=str(session.id),
    )
    await db.commit()
    active_sessions_gauge.dec()
    await db.refresh(session, attribute_names=["languages"])

    # Publish kill-switch so listener WebSocket handlers disconnect immediately.
    settings = get_settings()
    control_channel = f"session:{session_id}:control"
    try:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis.publish(
            control_channel,
            json.dumps({"type": "session_ended", "session_id": str(session_id)}),
        )
        await redis.aclose()
    except Exception:
        logging.getLogger(__name__).warning(
            "failed to publish kill-switch for session %s", session_id
        )

    return _session_out(session)


# ---------------------------------------------------------------------------
# Anonymous listener endpoint (read-only, scoped by join code)
# ---------------------------------------------------------------------------


@router.get("/join/{code}", response_model=ListenerSessionOut)
async def join_session(
    code: str,
    request: Request,
    db: DbSession,
    limiter: Annotated[SlidingWindowRateLimiter, Depends(get_join_rate_limiter)],
) -> ListenerSessionOut:
    """Look up a session by join code or join slug. No auth required.

    Returns only the information a listener needs -- no write paths, no
    internal IDs beyond the session UUID needed to open a WebSocket.
    Rate-limited per IP to deter join-code enumeration.
    """
    ip = client_identifier(request)
    result = limiter.check(f"join:{ip}")
    if not result.allowed:
        retry_after = max(1, int(result.retry_after_seconds) + 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many join attempts",
            headers={"Retry-After": str(retry_after)},
        )
    stmt = (
        select(Session)
        .options(selectinload(Session.languages))
        .where((Session.join_code == code) | (Session.join_slug == code))
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")

    return ListenerSessionOut(
        id=session.id,
        name=session.name,
        source_language=session.source_language,
        status=session.status.value,
        target_languages=[
            LanguageOut(language_code=lang.language_code, tts_enabled=lang.tts_enabled)
            for lang in session.languages
        ],
        started_at=session.started_at,
    )
