"""TTS synthesis and caching service.

Synthesizes audio for translation segments when TTS is enabled on the
target language.  Audio is cached on disk keyed by
``sha256(text + lang + voice)`` with a TTL matching the content retention
window (default 48 h).  The retention service calls :func:`evict_expired`
to purge stale entries.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.segment import TranslationSegment
from app.models.session import SessionLanguage

if TYPE_CHECKING:
    from app.providers.base import TTSProvider

logger = logging.getLogger(__name__)

# File extension per audio encoding.
_EXT_MAP: dict[str, str] = {
    "MP3": ".mp3",
    "OGG_OPUS": ".ogg",
    "LINEAR16": ".wav",
}


def cache_key(text: str, language: str, voice: str = "") -> str:
    """Deterministic cache key: hex digest of text+lang+voice."""
    payload = f"{text}\x00{language}\x00{voice}"
    return hashlib.sha256(payload.encode()).hexdigest()


class TTSCache:
    """Disk-backed TTS audio cache with TTL.

    Each entry is stored as ``<cache_dir>/<hex_digest>.<ext>`` alongside a
    ``<hex_digest>.meta`` file containing the creation timestamp.  Entries
    older than *ttl_seconds* are eligible for eviction.
    """

    def __init__(self, cache_dir: str, ttl_seconds: int, audio_encoding: str = "MP3") -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_seconds
        self._ext = _EXT_MAP.get(audio_encoding, ".mp3")

    def get(self, key: str) -> bytes | None:
        """Return cached audio bytes or *None* if missing/expired."""
        audio_path = self._dir / f"{key}{self._ext}"
        meta_path = self._dir / f"{key}.meta"

        if not audio_path.exists() or not meta_path.exists():
            return None

        # Check TTL.
        try:
            created_ts = float(meta_path.read_text().strip())
        except (ValueError, OSError):
            return None

        if time.time() - created_ts > self._ttl:
            self._remove(key)
            return None

        return audio_path.read_bytes()

    def put(self, key: str, audio: bytes) -> None:
        """Store audio bytes under *key*."""
        audio_path = self._dir / f"{key}{self._ext}"
        meta_path = self._dir / f"{key}.meta"
        audio_path.write_bytes(audio)
        meta_path.write_text(str(time.time()))

    def evict_expired(self) -> int:
        """Remove all entries older than TTL. Returns count of entries removed."""
        now = time.time()
        removed = 0
        for meta_path in self._dir.glob("*.meta"):
            try:
                created_ts = float(meta_path.read_text().strip())
            except (ValueError, OSError):
                meta_path.unlink(missing_ok=True)
                continue

            if now - created_ts > self._ttl:
                key = meta_path.stem
                self._remove(key)
                removed += 1

        return removed

    def _remove(self, key: str) -> None:
        audio_path = self._dir / f"{key}{self._ext}"
        meta_path = self._dir / f"{key}.meta"
        audio_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)

    @property
    def content_type(self) -> str:
        return {
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
            ".wav": "audio/wav",
        }.get(self._ext, "application/octet-stream")


class TTSService:
    """Orchestrates TTS synthesis, caching, and segment lookup.

    Wired into the translation fan-out pipeline: after a translation is
    persisted, call :meth:`synthesize_for_segment` to generate audio
    (if TTS is enabled on that language).  Listeners fetch audio via
    :meth:`get_audio_for_segment`.
    """

    def __init__(
        self,
        *,
        provider: TTSProvider,
        cache: TTSCache,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._db = db_sessionmaker

    async def synthesize_for_segment(
        self,
        translation_segment_id: int,
        text: str,
        language: str,
    ) -> str:
        """Synthesize (or serve from cache) and return the cache key."""
        key = cache_key(text, language)

        # Check cache first.
        if self._cache.get(key) is not None:
            logger.debug("tts cache hit key=%s lang=%s", key[:12], language)
            return key

        # Synthesize.
        audio = await self._provider.synthesize(text, language)
        if isinstance(audio, str):
            # Provider returned a URL — not expected for Google TTS but
            # the protocol allows it.  Skip caching.
            return key

        self._cache.put(key, audio)
        logger.debug("tts cache miss key=%s lang=%s bytes=%d", key[:12], language, len(audio))
        return key

    async def get_audio_for_segment(self, segment_id: int) -> tuple[bytes | None, str]:
        """Look up a TranslationSegment by ID, return (audio_bytes, content_type).

        Returns ``(None, content_type)`` if the segment doesn't exist, TTS is
        not enabled for that language, or synthesis hasn't completed yet.
        """
        async with self._db() as db:
            seg = await db.get(TranslationSegment, segment_id)
            if seg is None:
                return None, self._cache.content_type

            # Check that TTS is enabled for this language in this session.
            result = await db.execute(
                select(SessionLanguage).where(
                    SessionLanguage.session_id == seg.session_id,
                    SessionLanguage.language_code == seg.language_code,
                )
            )
            lang_row = result.scalar_one_or_none()
            if lang_row is None or not lang_row.tts_enabled:
                return None, self._cache.content_type

        key = cache_key(seg.text, seg.language_code)
        audio = self._cache.get(key)
        return audio, self._cache.content_type

    def evict_expired(self) -> int:
        """Proxy to cache eviction for use by the retention scheduler."""
        return self._cache.evict_expired()
