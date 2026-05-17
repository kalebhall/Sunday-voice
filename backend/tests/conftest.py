"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

# ---------------------------------------------------------------------------
# SQLite compatibility patch — must happen before any engine is created.
# PostgreSQL's JSONB type has no SQLite equivalent; map it to JSON so that
# Base.metadata.create_all works against an in-memory SQLite database.
# ---------------------------------------------------------------------------
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore[attr-defined]

# Set test env vars before importing the app so settings pick them up.
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-used-in-prod-32bytes!!")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("RETENTION_CLEANUP_ENABLED", "false")
os.environ.setdefault("TTS_ENABLED", "false")

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.models as _models  # noqa: F401 — registers all models with Base.metadata
from app.api.deps import reset_login_rate_limiter
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import Role, User  # Role for seeding; User for fixture type hints


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    # Ensure every test reads the current environment.
    get_settings.cache_clear()
    reset_login_rate_limiter()
    yield
    get_settings.cache_clear()
    reset_login_rate_limiter()


@pytest_asyncio.fixture
async def db_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create all application tables in an in-memory SQLite database.

    The SQLiteTypeCompiler patch at module level lets PostgreSQL-specific
    column types (JSONB → JSON, PG_UUID → CHAR(32)) degrade gracefully so
    that every model table — including audit_logs, usage_meters, etc. — is
    available to tests without requiring a real PostgreSQL instance.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    # Seed canonical roles.
    async with maker() as session:
        session.add(Role(name="admin", description="Full system access"))
        session.add(Role(name="operator", description="Create and run sessions"))
        await session.commit()

    try:
        yield maker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[TestClient]:
    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    try:
        with TestClient(app) as tc:
            yield tc
    finally:
        app.dependency_overrides.pop(get_session, None)
