"""Structured audit log helper for Sunday Voice.

Every privileged action (login, session lifecycle, user management, config
changes) should call :func:`write_audit_log`.  It does two things:

1. Emits a structured JSON log line (via the standard logging machinery) so
   the event is immediately visible in journald / any log shipper.
2. Inserts an ``AuditLog`` row into the database.  The caller is responsible
   for committing the session; this keeps audit writes inside the same
   transaction as the business operation, so they are either both committed
   or both rolled back.

For WebSocket handlers that don't already hold a DB session, use
:func:`write_audit_log_bg` which opens its own short-lived session.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit import AuditLog

_logger = logging.getLogger("app.audit")


def _emit_log(
    action: str,
    actor_user_id: int | None,
    target_type: str | None,
    target_id: str | None,
    ip_address: str | None,
    details: dict[str, Any] | None,
) -> None:
    _logger.info(
        "audit_event",
        extra={
            "audit_action": action,
            "actor_user_id": actor_user_id,
            "target_type": target_type,
            "target_id": target_id,
            "ip_address": ip_address,
            "details": details,
        },
    )


def write_audit_log(
    db: AsyncSession,
    *,
    action: str,
    actor_user_id: int | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an AuditLog row to *db* and emit a structured log line.

    Does **not** commit.  The caller must commit the session after this call
    (typically alongside the main business operation in the same transaction).
    """
    _emit_log(action, actor_user_id, target_type, target_id, ip_address, details)
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details=details,
        )
    )


async def write_audit_log_bg(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    action: str,
    actor_user_id: int | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an audit log in a standalone transaction.

    Use this from WebSocket handlers or background tasks that do not already
    hold a shared DB session.  Failures are logged and swallowed so that a
    DB hiccup does not close an active WebSocket connection.
    """
    _emit_log(action, actor_user_id, target_type, target_id, ip_address, details)
    try:
        async with db_sessionmaker() as db:
            db.add(
                AuditLog(
                    actor_user_id=actor_user_id,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    details=details,
                )
            )
            await db.commit()
    except Exception:
        _logger.exception("failed to persist audit log for action=%s", action)
