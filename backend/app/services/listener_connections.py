"""Per-IP connection cap for anonymous listener WebSockets.

Prevents a single client from opening an excessive number of WebSocket
connections (intentional or accidental).  Tracked in-process — sufficient
for the single-instance deployment target.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


class ListenerConnectionTracker:
    """Thread-safe per-IP connection counter with async context-manager helper."""

    def __init__(self, max_per_ip: int = 10) -> None:
        self._max_per_ip = max_per_ip
        self._counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def max_per_ip(self) -> int:
        return self._max_per_ip

    async def try_acquire(self, ip: str) -> bool:
        """Increment the counter for *ip*.  Returns False if cap exceeded."""
        async with self._lock:
            current = self._counts.get(ip, 0)
            if current >= self._max_per_ip:
                return False
            self._counts[ip] = current + 1
            return True

    async def release(self, ip: str) -> None:
        """Decrement the counter for *ip*."""
        async with self._lock:
            current = self._counts.get(ip, 0)
            if current <= 1:
                self._counts.pop(ip, None)
            else:
                self._counts[ip] = current - 1

    @asynccontextmanager
    async def track(self, ip: str) -> AsyncIterator[bool]:
        """Context manager that acquires on entry, releases on exit.

        Yields ``True`` if the connection was accepted, ``False`` if the cap
        was exceeded (caller should close the socket).
        """
        acquired = await self.try_acquire(ip)
        try:
            yield acquired
        finally:
            if acquired:
                await self.release(ip)

    def connection_count(self, ip: str) -> int:
        return self._counts.get(ip, 0)

    def reset(self) -> None:
        """Clear all tracking state.  Used in tests."""
        self._counts.clear()


# Module-level singleton.
listener_connections = ListenerConnectionTracker()
