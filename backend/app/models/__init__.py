"""ORM models.

All models are imported here so that ``Base.metadata`` is fully populated
whenever any caller imports :mod:`app.models` (Alembic env, tests, app startup).
"""

from app.models.audit import AuditLog
from app.models.config import SystemConfig
from app.models.feedback import TranslationFeedback
from app.models.role import ROLE_ADMIN, ROLE_OPERATOR, Role
from app.models.segment import TranscriptSegment, TranslationSegment
from app.models.session import (
    AudioTransport,
    Session,
    SessionLanguage,
    SessionStatus,
)
from app.models.usage import UsageMeter
from app.models.user import User

__all__ = [
    "ROLE_ADMIN",
    "ROLE_OPERATOR",
    "AudioTransport",
    "AuditLog",
    "Role",
    "Session",
    "SessionLanguage",
    "SessionStatus",
    "SystemConfig",
    "TranscriptSegment",
    "TranslationFeedback",
    "TranslationSegment",
    "UsageMeter",
    "User",
]
