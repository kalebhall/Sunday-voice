"""Translation fan-out service.

Subscribes to in-process TranscriptEvent pub/sub, translates each segment
into all enabled target languages concurrently, persists TranslationSegment
rows, and publishes results to Redis pub/sub keyed by
``session:{id}:lang:{code}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.segment import TranscriptSegment, TranslationSegment
from app.models.session import SessionLanguage
from app.providers.base import CostMeter, TranslationProvider
from app.services.pubsub import TranscriptEvent, transcript_pubsub

if TYPE_CHECKING:
    from app.services.tts import TTSService

logger = logging.getLogger(__name__)


class TranslationFanout:
    """Consumes transcript events and fans out translations.

    For each :class:`TranscriptEvent`:
    1. Looks up enabled target languages for the session.
    2. Translates concurrently to all target languages (skipping source).
    3. Persists :class:`TranslationSegment` rows (48h TTL via retention service).
    4. Publishes each translation to Redis pub/sub
       ``session:{session_id}:lang:{code}``.
    5. Meters character usage via :class:`CostMeter`.
    """

    def __init__(
        self,
        *,
        translation_provider: TranslationProvider,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,  # type: ignore[type-arg]
        cost_meter: CostMeter | None = None,
        tts_service: TTSService | None = None,
    ) -> None:
        self._provider = translation_provider
        self._db_sessionmaker = db_sessionmaker
        self._redis = redis
        self._cost_meter = cost_meter
        self._tts_service = tts_service
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    async def start(self, session_id: UUID) -> None:
        """Begin consuming transcript events for *session_id*."""
        if session_id in self._tasks:
            logger.warning("translation fanout already running for session %s", session_id)
            return

        task = asyncio.create_task(
            self._consume_loop(session_id),
            name=f"translation-fanout-{session_id}",
        )
        self._tasks[session_id] = task
        logger.info("translation fanout started for session %s", session_id)

    async def stop(self, session_id: UUID, timeout: float = 10.0) -> None:
        """Stop consuming for *session_id*."""
        task = self._tasks.pop(session_id, None)
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.CancelledError, TimeoutError):
            pass
        logger.info("translation fanout stopped for session %s", session_id)

    async def stop_all(self) -> None:
        """Stop all active fanout tasks."""
        session_ids = list(self._tasks.keys())
        for sid in session_ids:
            await self.stop(sid)

    # -- Internal --------------------------------------------------------------

    async def _consume_loop(self, session_id: UUID) -> None:
        """Subscribe to transcript events and process each one."""
        pubsub = await transcript_pubsub.get_or_create(session_id)
        sub_id, queue = await pubsub.subscribe()

        try:
            while True:
                event = await queue.get()
                try:
                    await self._handle_event(event)
                except Exception:
                    logger.exception(
                        "translation fanout error for session %s seq %d",
                        event.session_id,
                        event.sequence,
                    )
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(sub_id)
            await transcript_pubsub.remove_if_empty(session_id)

    async def _handle_event(self, event: TranscriptEvent) -> None:
        """Translate a single transcript event to all target languages."""
        target_languages = await self._get_target_languages(event.session_id)

        # Skip source language — no translation needed.
        languages_to_translate = [
            lang for lang in target_languages if lang != event.language
        ]

        if not languages_to_translate:
            return

        # Look up the TranscriptSegment row (needed for FK).
        transcript_segment_id = await self._get_transcript_segment_id(
            event.session_id, event.sequence
        )

        # Fan out translations concurrently.
        results = await asyncio.gather(
            *(
                self._translate_one(event, lang, transcript_segment_id)
                for lang in languages_to_translate
            ),
            return_exceptions=True,
        )

        for lang, result in zip(languages_to_translate, results):
            if isinstance(result, Exception):
                logger.error(
                    "translation to %s failed for session %s seq %d: %s",
                    lang,
                    event.session_id,
                    event.sequence,
                    result,
                )

    async def _translate_one(
        self,
        event: TranscriptEvent,
        target_language: str,
        transcript_segment_id: int | None,
    ) -> None:
        """Translate, persist, optionally synthesize TTS, and publish."""
        translated_text = await self._provider.translate(
            text=event.text,
            source_language=event.language,
            target_language=target_language,
        )

        # Persist TranslationSegment.
        translation_segment_id: int | None = None
        if transcript_segment_id is not None:
            translation_segment_id = await self._persist_translation(
                session_id=event.session_id,
                transcript_segment_id=transcript_segment_id,
                language_code=target_language,
                text=translated_text,
            )

        # Synthesize TTS in the background (non-blocking for publish).
        tts_url: str | None = None
        if self._tts_service and translation_segment_id is not None:
            tts_enabled = await self._is_tts_enabled(event.session_id, target_language)
            if tts_enabled:
                try:
                    await self._tts_service.synthesize_for_segment(
                        translation_segment_id=translation_segment_id,
                        text=translated_text,
                        language=target_language,
                    )
                    tts_url = f"/api/tts/{translation_segment_id}"
                except Exception:
                    logger.exception(
                        "TTS synthesis failed for segment %d lang=%s",
                        translation_segment_id,
                        target_language,
                    )

        # Publish to Redis pub/sub.
        channel = f"session:{event.session_id}:lang:{target_language}"
        msg: dict = {
            "session_id": str(event.session_id),
            "sequence": event.sequence,
            "language": target_language,
            "text": translated_text,
            "source_language": event.language,
        }
        if translation_segment_id is not None:
            msg["segment_id"] = translation_segment_id
        if tts_url:
            msg["tts_url"] = tts_url
        await self._redis.publish(channel, json.dumps(msg))

        logger.debug(
            "published translation seq=%d lang=%s session=%s tts=%s",
            event.sequence,
            target_language,
            event.session_id,
            bool(tts_url),
        )

    async def _get_target_languages(self, session_id: UUID) -> list[str]:
        """Fetch enabled target language codes for a session."""
        async with self._db_sessionmaker() as db:
            result = await db.execute(
                select(SessionLanguage.language_code).where(
                    SessionLanguage.session_id == session_id
                )
            )
            return list(result.scalars().all())

    async def _get_transcript_segment_id(
        self, session_id: UUID, sequence: int
    ) -> int | None:
        """Look up the transcript segment PK by session + sequence."""
        async with self._db_sessionmaker() as db:
            result = await db.execute(
                select(TranscriptSegment.id).where(
                    TranscriptSegment.session_id == session_id,
                    TranscriptSegment.sequence == sequence,
                )
            )
            return result.scalar_one_or_none()

    async def _is_tts_enabled(self, session_id: UUID, language_code: str) -> bool:
        """Check if TTS is enabled for a language in a session."""
        async with self._db_sessionmaker() as db:
            result = await db.execute(
                select(SessionLanguage.tts_enabled).where(
                    SessionLanguage.session_id == session_id,
                    SessionLanguage.language_code == language_code,
                )
            )
            val = result.scalar_one_or_none()
            return bool(val)

    async def _persist_translation(
        self,
        *,
        session_id: UUID,
        transcript_segment_id: int,
        language_code: str,
        text: str,
    ) -> int:
        """Insert a TranslationSegment row. Returns the new segment ID."""
        async with self._db_sessionmaker() as db:
            segment = TranslationSegment(
                session_id=session_id,
                transcript_segment_id=transcript_segment_id,
                language_code=language_code,
                text=text,
                provider="google",
            )
            db.add(segment)
            await db.commit()
            await db.refresh(segment)
            return segment.id
