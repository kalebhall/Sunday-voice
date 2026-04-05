"""Provider interfaces per docs/architecture.md."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable


@runtime_checkable
class TranscriptionProvider(Protocol):
    async def transcribe_stream(
        self,
        audio_stream: AsyncIterator[bytes],
        source_language: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield transcript segments as they become available."""
        ...


@runtime_checkable
class TranslationProvider(Protocol):
    async def translate(
        self, text: str, source_language: str, target_language: str
    ) -> str:
        """Translate a single segment."""
        ...


@runtime_checkable
class TTSProvider(Protocol):
    async def synthesize(self, text: str, language: str) -> bytes | str:
        """Synthesize speech for a segment; returns audio bytes or a URL."""
        ...


@runtime_checkable
class CostMeter(Protocol):
    async def record(self, provider: str, operation: str, units: float) -> None:
        """Record provider usage for cost accounting."""
        ...
