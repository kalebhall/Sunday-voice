"""FastAPI application entrypoint for Sunday Voice."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import RequestIDMiddleware
from app.db.session import get_sessionmaker
from app.services.retention import run_retention_cleanup
from app.services.scheduler import PeriodicTask, Scheduler
from app.ws import ws_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application startup and shutdown hooks."""
    settings = get_settings()
    scheduler = Scheduler()

    if settings.retention_cleanup_enabled:
        sessionmaker = get_sessionmaker()
        retention_hours = settings.content_retention_hours
        interval_seconds = settings.retention_cleanup_interval_minutes * 60

        async def _retention_job() -> None:
            await run_retention_cleanup(sessionmaker, retention_hours)

        scheduler.add(
            PeriodicTask(
                name="retention-cleanup",
                job=_retention_job,
                interval_seconds=interval_seconds,
                initial_delay_seconds=min(30.0, interval_seconds),
            )
        )

    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        await scheduler.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.app_log_level)

    app = FastAPI(
        title="Sunday Voice",
        version="0.1.0",
        description="Real-time translation for in-building meetings.",
        lifespan=lifespan,
    )

    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix="/api")
    app.include_router(ws_router, prefix="/ws")

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness probe: process is running."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> dict[str, str]:
        """Readiness probe: app is ready to serve traffic.

        Dependency checks (DB, Redis, providers) will be wired in alongside
        their respective startup hooks.
        """
        return {"status": "ready"}

    return app


app = create_app()
