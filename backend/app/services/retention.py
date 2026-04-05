"""Retention and cleanup service.

Enforces the 48h content retention policy (see `docs/security-and-privacy.md`):
transcript and translation segments older than the retention window are deleted,
and sessions whose content window has closed are purged. Aggregate usage stats
in ``usage_meters`` are preserved via ``ON DELETE SET NULL`` on ``session_id``.

Every destructive pass writes an ``AuditLog`` entry with deletion counts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit import AuditLog
from app.models.segment import TranscriptSegment, TranslationSegment
from app.models.session import Session as MeetingSession

logger = logging.getLogger(__name__)

RETENTION_AUDIT_ACTION = "retention.cleanup"


@dataclass(slots=True)
class CleanupResult:
    transcript_segments_deleted: int
    translation_segments_deleted: int
    sessions_purged: int
    cutoff: datetime

    @property
    def total_segments(self) -> int:
        return self.transcript_segments_deleted + self.translation_segments_deleted

    @property
    def had_work(self) -> bool:
        return self.total_segments > 0 or self.sessions_purged > 0


async def _delete_expired_segments(
    db: AsyncSession, cutoff: datetime
) -> tuple[int, int]:
    """Delete segments older than ``cutoff``. Returns (transcript, translation)."""
    # Delete translations first to avoid relying solely on ON DELETE CASCADE
    # timing and to get accurate counts for audit.
    translation_result = await db.execute(
        delete(TranslationSegment).where(TranslationSegment.created_at < cutoff)
    )
    transcript_result = await db.execute(
        delete(TranscriptSegment).where(TranscriptSegment.created_at < cutoff)
    )
    return transcript_result.rowcount or 0, translation_result.rowcount or 0


async def _purge_expired_sessions(db: AsyncSession, cutoff: datetime) -> int:
    """Purge sessions whose retention window has fully elapsed.

    A session is eligible when either:
      - ``expires_at`` is set and has passed, OR
      - the session has ended and ``ended_at`` is older than ``cutoff``.

    Segments are removed via ``ON DELETE CASCADE``; usage rows retain their
    aggregate totals with ``session_id`` nulled out.
    """
    now = datetime.now(UTC)
    stmt = select(MeetingSession.id).where(
        or_(
            and_(
                MeetingSession.expires_at.is_not(None),
                MeetingSession.expires_at < now,
            ),
            and_(
                MeetingSession.ended_at.is_not(None),
                MeetingSession.ended_at < cutoff,
            ),
        )
    )
    session_ids = list((await db.execute(stmt)).scalars().all())
    if not session_ids:
        return 0

    result = await db.execute(
        delete(MeetingSession).where(MeetingSession.id.in_(session_ids))
    )
    return result.rowcount or 0


async def run_retention_cleanup(
    sessionmaker: async_sessionmaker[AsyncSession],
    retention_hours: int,
) -> CleanupResult:
    """Execute one retention pass and write an audit entry."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=retention_hours)

    async with sessionmaker() as db:
        async with db.begin():
            transcripts, translations = await _delete_expired_segments(db, cutoff)
            sessions_purged = await _purge_expired_sessions(db, cutoff)
            result = CleanupResult(
                transcript_segments_deleted=transcripts,
                translation_segments_deleted=translations,
                sessions_purged=sessions_purged,
                cutoff=cutoff,
            )
            if result.had_work:
                db.add(
                    AuditLog(
                        actor_user_id=None,
                        action=RETENTION_AUDIT_ACTION,
                        target_type="retention",
                        target_id=None,
                        details={
                            "cutoff": cutoff.isoformat(),
                            "retention_hours": retention_hours,
                            "transcript_segments_deleted": transcripts,
                            "translation_segments_deleted": translations,
                            "sessions_purged": sessions_purged,
                        },
                    )
                )

    if result.had_work:
        logger.info(
            "retention.cleanup cutoff=%s transcripts=%d translations=%d sessions=%d",
            cutoff.isoformat(),
            transcripts,
            translations,
            sessions_purged,
        )
    else:
        logger.debug("retention.cleanup no-op cutoff=%s", cutoff.isoformat())
    return result
