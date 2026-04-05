"""Asyncio-based periodic task scheduler.

Lightweight alternative to APScheduler for running background jobs inside
the FastAPI event loop. Keeps the dependency footprint small for the LXC
deployment described in ``docs/deployment.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

JobFunc = Callable[[], Awaitable[None]]


class PeriodicTask:
    """Runs ``job`` every ``interval_seconds`` until cancelled."""

    def __init__(
        self,
        name: str,
        job: JobFunc,
        interval_seconds: float,
        *,
        initial_delay_seconds: float = 0.0,
    ) -> None:
        self.name = name
        self._job = job
        self._interval = interval_seconds
        self._initial_delay = initial_delay_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name=f"periodic:{self.name}")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        if self._initial_delay > 0:
            try:
                await asyncio.sleep(self._initial_delay)
            except asyncio.CancelledError:
                return
        while True:
            try:
                await self._job()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("periodic task %s failed", self.name)
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return


class Scheduler:
    """Owns a set of periodic tasks for the app lifespan."""

    def __init__(self) -> None:
        self._tasks: list[PeriodicTask] = []

    def add(self, task: PeriodicTask) -> None:
        self._tasks.append(task)

    def start(self) -> None:
        for task in self._tasks:
            task.start()
            logger.info("scheduler: started task %s", task.name)

    async def stop(self) -> None:
        for task in self._tasks:
            await task.stop()
            logger.info("scheduler: stopped task %s", task.name)
