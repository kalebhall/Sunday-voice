"""Translation feedback model.

Feedback records survive the 48-hour content retention purge — they are
metadata, not content.  session_id and segment_id are stored without FK
constraints so they remain valid after the referenced rows are deleted.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class TranslationFeedback(TimestampMixin, Base):
    """Anonymized quality signal (thumbs-down) for a translated segment.

    Only metadata is stored — no transcript or audio content.  Survives the
    48-hour content purge so admins can review translation quality trends.
    """

    __tablename__ = "translation_feedback"
    __table_args__ = (
        Index("ix_feedback_session_lang", "session_id", "language_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Intentionally no FK constraints — these references must survive the
    # 48-hour content purge that deletes sessions and translation segments.
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    language_code: Mapped[str] = mapped_column(String(16), nullable=False)
    segment_id: Mapped[int] = mapped_column(Integer, nullable=False)
