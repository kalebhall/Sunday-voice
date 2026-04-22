"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

# Set test env vars before importing the app so settings pick them up.
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-used-in-prod-32bytes!!")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("RETENTION_CLEANUP_ENABLED", "false")

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import reset_login_rate_limiter
from app.core.config import get_settings
from app.db.session import get_session
from app.main import app
from app.models import AuditLog, Role, Session, SessionLanguage, User


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
    """Create tables in an in-memory sqlite DB.

    Uses checkfirst=True so Postgres-specific dialect types degrade
    gracefully (PG_UUID -> CHAR(32), etc.).
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        for tbl in (Role, User, Session, SessionLanguage, AuditLog):
            await conn.run_sync(
                lambda sync_conn, t=tbl: t.__table__.create(sync_conn, checkfirst=True)
            )
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
