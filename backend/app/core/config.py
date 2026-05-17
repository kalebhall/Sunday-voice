"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file so it's found regardless of cwd.
# config.py lives at backend/app/core/config.py → 3 parents up = project root.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = _PROJECT_ROOT / ".env"


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
    whisper_model: str = "gpt-4o-mini-transcribe"
    # Max simultaneous transcription API calls across all sessions.
    # Tune upward if API headroom allows; lower if you hit 429s.
    whisper_max_concurrent: int = 5
    # Sliding overlap window (bytes of WebM/Opus audio, ~16 KB/s).
    # window: how much trailing audio is re-transcribed each call — larger
    #   gives the model more sentence context and higher accuracy.
    # slide: how much fresh audio accumulates between calls — also the added
    #   latency.  Each call resends (window/slide) of overlapping audio, so
    #   that ratio is also the transcription-cost multiplier.
    whisper_window_bytes: int = 240_000  # ~15 s of context
    whisper_slide_bytes: int = 48_000  # ~3 s latency, ~5x cost
    # Domain vocabulary fed to the model as a prompt to bias spelling of
    # names and jargon.  Most effective in the meeting's spoken language.
    whisper_prompt: str = (
        "Transcript of a Latter-day Saint ward or stake meeting. Common terms: "
        "ward, stake, sacrament meeting, priesthood, bishop, bishopric, "
        "stake president, high council, Relief Society, elders quorum, "
        "Primary, Young Women, Young Men, testimony, covenant, ordinance, "
        "ministering, tithing, fast offering, Book of Mormon, "
        "Doctrine and Covenants, General Conference, First Presidency."
    )
    google_translate_api_key: str = ""
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

    # Static files: path to the built frontend dist directory.
    # Override via STATIC_DIR env var if the frontend is built elsewhere.
    static_dir: str = str(_PROJECT_ROOT / "frontend" / "dist")

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.app_cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
