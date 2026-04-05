"""Simple in-process sliding-window rate limiter.

Used to throttle unauthenticated endpoints (login) per client identifier. For
a single-instance self-hosted deployment this is sufficient; a Redis-backed
limiter can be substituted behind the same interface when horizontal scaling
is needed.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: float


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window counter keyed by arbitrary strings."""

    def __init__(self, *, max_requests: int, window_seconds: float) -> None:
        if max_requests <= 0:
            raise ValueError("max_requests must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def max_requests(self) -> int:
        return self._max_requests

    @property
    def window_seconds(self) -> float:
        return self._window_seconds

    def _now(self) -> float:
        return time.monotonic()

    def check(self, key: str) -> RateLimitResult:
        """Record a hit for ``key`` and return whether it is allowed."""
        now = self._now()
        cutoff = now - self._window_seconds
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._max_requests:
                retry_after = bucket[0] + self._window_seconds - now
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    retry_after_seconds=max(retry_after, 0.0),
                )
            bucket.append(now)
            return RateLimitResult(
                allowed=True,
                remaining=self._max_requests - len(bucket),
                retry_after_seconds=0.0,
            )

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
