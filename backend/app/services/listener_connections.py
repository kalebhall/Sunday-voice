"""Per-IP and per-session connection caps for anonymous listener WebSockets.

Prevents a single IP address or a single session from accumulating an excessive
number of concurrent WebSocket connections (intentional or accidental).
Tracked in-process — sufficient for the single-instance deployment target.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


class ListenerConnectionTracker:
    """Thread-safe per-IP and per-session connection counter.

    ``track()`` is the primary interface: an async context manager that
    acquires slots on entry and releases them on exit.  It yields a tuple
    ``(allowed: bool, reason: str)``; *reason* is an empty string when
    *allowed* is True.
    """

    def __init__(self, max_per_ip: int = 10, max_per_session: int = 100) -> None:
        self._max_per_ip = max_per_ip
        self._max_per_session = max_per_session
        self._ip_counts: dict[str, int] = {}
        self._session_counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def max_per_ip(self) -> int:
        return self._max_per_ip

    @property
    def max_per_session(self) -> int:
        return self._max_per_session

    async def try_acquire(
        self, ip: str, session_key: str | None = None
    ) -> tuple[bool, str]:
        """Attempt to claim slots for *ip* (and optionally *session_key*).

        Returns ``(True, "")`` on success or ``(False, reason)`` when a cap
        is exceeded.  On failure no state is mutated.
        """
        async with self._lock:
            if self._ip_counts.get(ip, 0) >= self._max_per_ip:
                return False, "too many connections from this IP"
            if (
                session_key is not None
                and self._session_counts.get(session_key, 0) >= self._max_per_session
            ):
                return False, "too many listeners for this session"
            # Commit both slots atomically.
            self._ip_counts[ip] = self._ip_counts.get(ip, 0) + 1
            if session_key is not None:
                self._session_counts[session_key] = (
                    self._session_counts.get(session_key, 0) + 1
                )
            return True, ""

    async def release(self, ip: str, session_key: str | None = None) -> None:
        """Release previously acquired slots."""
        async with self._lock:
            ip_count = self._ip_counts.get(ip, 0)
            if ip_count <= 1:
                self._ip_counts.pop(ip, None)
            else:
                self._ip_counts[ip] = ip_count - 1
            if session_key is not None:
                sess_count = self._session_counts.get(session_key, 0)
                if sess_count <= 1:
                    self._session_counts.pop(session_key, None)
                else:
                    self._session_counts[session_key] = sess_count - 1

    @asynccontextmanager
    async def track(
        self, ip: str, session_key: str | None = None
    ) -> AsyncIterator[tuple[bool, str]]:
        """Context manager: acquire on entry, release on exit.

        Yields ``(True, "")`` if both caps allow the connection, or
        ``(False, reason)`` if either cap is exceeded (caller should close
        the socket; no slots are held on rejection).
        """
        allowed, reason = await self.try_acquire(ip, session_key)
        try:
            yield allowed, reason
        finally:
            if allowed:
                await self.release(ip, session_key)

    def connection_count(self, ip: str) -> int:
        return self._ip_counts.get(ip, 0)

    def session_connection_count(self, session_key: str) -> int:
        return self._session_counts.get(session_key, 0)

    def reset(self) -> None:
        """Clear all tracking state.  Used in tests."""
        self._ip_counts.clear()
        self._session_counts.clear()


# Module-level singleton.
listener_connections = ListenerConnectionTracker()
