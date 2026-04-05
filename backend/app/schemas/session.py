"""Pydantic schemas for session endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class LanguageConfig(BaseModel):
    language_code: str = Field(min_length=2, max_length=16)
    tts_enabled: bool = False


class SessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_language: str = Field(default="en", min_length=2, max_length=16)
    audio_transport: str = Field(default="websocket_chunks")
    target_languages: list[LanguageConfig] = Field(default_factory=list, max_length=10)
    scheduled_at: datetime | None = None


class SessionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    source_language: str | None = Field(default=None, min_length=2, max_length=16)
    audio_transport: str | None = None
    target_languages: list[LanguageConfig] | None = Field(default=None, max_length=10)


class LanguageOut(BaseModel):
    language_code: str
    tts_enabled: bool


class SessionOut(BaseModel):
    id: uuid.UUID
    name: str
    join_slug: str
    join_code: str | None
    join_url: str
    source_language: str
    status: str
    audio_transport: str
    target_languages: list[LanguageOut]
    scheduled_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    created_by_user_id: int


class SessionListOut(BaseModel):
    sessions: list[SessionOut]
    count: int


class ListenerSessionOut(BaseModel):
    """Read-only session info for anonymous listeners."""

    id: uuid.UUID
    name: str
    source_language: str
    status: str
    target_languages: list[LanguageOut]
    started_at: datetime | None
