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
from app.services.listener_connections import listener_connections
from app.services.retention import run_retention_cleanup
from app.services.scheduler import PeriodicTask, Scheduler
from app.services.tts import TTSCache, TTSService
from app.ws import ws_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application startup and shutdown hooks."""
    settings = get_settings()
    scheduler = Scheduler()
    sessionmaker = get_sessionmaker()

    if settings.retention_cleanup_enabled:
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

    # TTS service (optional — only when tts_enabled is True).
    if settings.tts_enabled:
        from app.providers.google_tts import GoogleTTSProvider

        voice_overrides: dict[str, str] = {}
        if settings.tts_voice_overrides:
            for pair in settings.tts_voice_overrides.split(","):
                pair = pair.strip()
                if "=" in pair:
                    lang, voice = pair.split("=", 1)
                    voice_overrides[lang.strip()] = voice.strip()

        tts_provider = GoogleTTSProvider(
            audio_encoding=settings.tts_audio_encoding,
            voice_overrides=voice_overrides or None,
        )
        tts_cache = TTSCache(
            cache_dir=settings.tts_cache_dir,
            ttl_seconds=settings.content_retention_hours * 3600,
            audio_encoding=settings.tts_audio_encoding,
        )
        tts_service = TTSService(
            provider=tts_provider,
            cache=tts_cache,
            db_sessionmaker=sessionmaker,
        )
        app.state.tts_service = tts_service

        # Periodic cache eviction aligned with retention.
        import logging as _logging
        _tts_logger = _logging.getLogger(__name__)

        async def _tts_evict_job() -> None:
            removed = tts_service.evict_expired()
            if removed:
                _tts_logger.info("tts cache eviction removed %d entries", removed)

        scheduler.add(
            PeriodicTask(
                name="tts-cache-eviction",
                job=_tts_evict_job,
                interval_seconds=settings.retention_cleanup_interval_minutes * 60,
                initial_delay_seconds=min(60.0, settings.retention_cleanup_interval_minutes * 60),
            )
        )

    # Apply listener connection caps from settings.
    listener_connections._max_per_ip = settings.listener_max_connections_per_ip
    listener_connections._max_per_session = settings.listener_max_connections_per_session

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
