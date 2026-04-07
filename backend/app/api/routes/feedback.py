"""Translation quality feedback endpoint.

POST /api/feedback — anonymous, rate-limited.

Stores a thumbs-down signal for a translation segment without persisting
any transcript content.  Only metadata is stored: session_id, language_code,
segment_id, and timestamp.  Records survive the 48-hour content purge.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import DbSession
from app.core.rate_limit import SlidingWindowRateLimiter
from app.models.feedback import TranslationFeedback

router = APIRouter()

# 60 feedback events per IP per 5-minute window — generous but abuse-resistant.
_feedback_limiter = SlidingWindowRateLimiter(max_requests=60, window_seconds=300)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


class FeedbackCreate(BaseModel):
    segment_id: int = Field(gt=0, description="TranslationSegment.id")
    language_code: str = Field(min_length=2, max_length=16)
    session_id: uuid.UUID


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    payload: FeedbackCreate,
    db: DbSession,
    request: Request,
) -> dict[str, bool]:
    """Record a thumbs-down quality signal for a translated segment.

    Anonymous endpoint — no auth required.  Read-only path: stores only
    metadata, no transcript or audio content.
    """
    ip = _client_ip(request)
    result = _feedback_limiter.check(ip)
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many feedback submissions — please slow down",
            headers={"Retry-After": str(int(result.retry_after_seconds) + 1)},
        )

    feedback = TranslationFeedback(
        session_id=payload.session_id,
        language_code=payload.language_code,
        segment_id=payload.segment_id,
    )
    db.add(feedback)
    await db.commit()
    return {"ok": True}
