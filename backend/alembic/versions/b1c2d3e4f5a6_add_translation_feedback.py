"""add translation_feedback table

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-04-07 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "translation_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        # No FK — these references survive the 48-hour content purge.
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("language_code", sa.String(length=16), nullable=False),
        sa.Column("segment_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_feedback_session_lang",
        "translation_feedback",
        ["session_id", "language_code"],
    )
    op.create_index(
        "ix_translation_feedback_session_id",
        "translation_feedback",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_translation_feedback_session_id", table_name="translation_feedback")
    op.drop_index("ix_feedback_session_lang", table_name="translation_feedback")
    op.drop_table("translation_feedback")
