"""Session management endpoints (stub)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("")
async def list_sessions() -> list[dict[str, str]]:
    """List active sessions (stub)."""
    return []
