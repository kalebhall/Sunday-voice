"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file so it's found regardless of cwd.
# config.py lives at backend/app/core/config.py → 3 parents up = project root.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    """Typed application settings.

    Values are loaded from environment variables (and an optional .env file).
    See .env.example for the full list of knobs.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_base_url: str = "http://localhost:8000"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_log_level: str = "info"
    app_cors_origins: str = "http://localhost:5173"

    # Security
    # No default: SECRET_KEY must be set explicitly in the environment.
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"
    secret_key: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_minutes: int = 60
    jwt_refresh_token_ttl_days: int = 14

    # Login throttling (sliding window per client+email).
    login_rate_limit_max_attempts: int = 10
    login_rate_limit_window_seconds: float = 60.0

    # Database / Redis
    database_url: str = "postgresql+asyncpg://sundayvoice:sundayvoice@localhost:5432/sundayvoice"
    redis_url: str = "redis://localhost:6379/0"

    # Retention
    content_retention_hours: int = 48
    retention_cleanup_interval_minutes: int = 15
    retention_cleanup_enabled: bool = True

    # Providers
    openai_api_key: str = ""
    whisper_model: str = "whisper-1"
    # Max simultaneous Whisper API calls across all sessions.
    # Tune upward if Whisper headroom allows; lower if you hit 429s.
    whisper_max_concurrent: int = 5
    # Flush audio buffer to Whisper after this many bytes.  At ~16 KB/s
    # (Opus 128 kbps) a 2.5 s chunk is ~40 KB, so 32 KB triggers once per
    # chunk.  The original 1 MiB default batched ~25 chunks (62 s) — far too
    # large for real-time operation.
    whisper_chunk_flush_bytes: int = 32_768
    google_application_credentials: str = ""
    google_cloud_project: str = ""
    google_translate_location: str = "global"
    tts_enabled: bool = True
    tts_audio_encoding: str = "MP3"
    tts_voice_overrides: str = ""  # comma-separated "lang=voice" pairs, e.g. "es=es-US-Wavenet-B"
    tts_cache_dir: str = "/tmp/sunday-voice-tts-cache"

    # Listener WebSocket
    listener_scrollback_limit: int = 50
    listener_heartbeat_seconds: float = 15.0
    listener_max_connections_per_ip: int = 10
    listener_max_connections_per_session: int = 100

    # Join endpoint rate limit (anonymous, per IP)
    join_rate_limit_max_attempts: int = 30
    join_rate_limit_window_seconds: float = 60.0

    # Operator audio byte cap (rolling per-minute, per session)
    operator_audio_max_bytes_per_minute: int = 10 * 1024 * 1024  # 10 MB

    # Cost controls
    monthly_budget_usd: float = 50.0
    budget_alert_threshold: float = 0.8

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.app_cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
