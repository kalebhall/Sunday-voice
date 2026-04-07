"""add web_speech to audio_transport enum

Revision ID: f2a3b4c5d6e7
Revises: d1e2f3a4b5c6
Create Date: 2026-04-07 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostgreSQL allows adding enum values without a full type rebuild.
    # IF NOT EXISTS prevents errors on repeated runs (e.g. during tests).
    op.execute("ALTER TYPE audio_transport ADD VALUE IF NOT EXISTS 'web_speech'")


def downgrade() -> None:
    # PostgreSQL does not support removing individual enum values.
    # A full enum rebuild would be required and is intentionally omitted here
    # because rows using 'web_speech' would need to be migrated first.
    # To downgrade, delete or update any rows using audio_transport='web_speech'
    # before reverting to the previous revision manually.
    pass
