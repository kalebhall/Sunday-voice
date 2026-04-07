"""Meeting session models."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.segment import TranscriptSegment, TranslationSegment
    from app.models.user import User


class SessionStatus(enum.StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ENDED = "ended"


class AudioTransport(enum.StrEnum):
    WEBSOCKET_CHUNKS = "websocket_chunks"
    WEBRTC = "webrtc"
    WEB_SPEECH = "web_speech"


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Non-guessable public join slug (see security-and-privacy.md).
    join_slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Short human-enterable code for on-site join.
    join_code: Mapped[str | None] = mapped_column(
        String(12), unique=True, index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_language: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status"),
        default=SessionStatus.DRAFT,
        nullable=False,
        index=True,
    )
    audio_transport: Mapped[AudioTransport] = mapped_column(
        Enum(AudioTransport, name="audio_transport"),
        default=AudioTransport.WEBSOCKET_CHUNKS,
        nullable=False,
    )

    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_by: Mapped[User] = relationship(back_populates="sessions")

    languages: Mapped[list[SessionLanguage]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    transcript_segments: Mapped[list[TranscriptSegment]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    translation_segments: Mapped[list[TranslationSegment]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class SessionLanguage(TimestampMixin, Base):
    __tablename__ = "session_languages"
    __table_args__ = (
        UniqueConstraint("session_id", "language_code", name="uq_session_language"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    language_code: Mapped[str] = mapped_column(String(16), nullable=False)
    tts_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    session: Mapped[Session] = relationship(back_populates="languages")
