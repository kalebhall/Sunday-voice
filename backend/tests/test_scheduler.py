"""Tests for the lightweight periodic scheduler."""

from __future__ import annotations

import asyncio

import pytest

from app.services.scheduler import PeriodicTask, Scheduler


@pytest.mark.asyncio
async def test_periodic_task_runs_and_stops() -> None:
    calls = 0

    async def job() -> None:
        nonlocal calls
        calls += 1

    task = PeriodicTask("test", job, interval_seconds=0.01)
    task.start()
    await asyncio.sleep(0.05)
    await task.stop()
    assert calls >= 2


@pytest.mark.asyncio
async def test_periodic_task_survives_job_exceptions() -> None:
    calls = 0

    async def job() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    task = PeriodicTask("flaky", job, interval_seconds=0.01)
    task.start()
    await asyncio.sleep(0.05)
    await task.stop()
    assert calls >= 2


@pytest.mark.asyncio
async def test_scheduler_starts_and_stops_all_tasks() -> None:
    counts = {"a": 0, "b": 0}

    async def make_job(key: str) -> None:
        counts[key] += 1

    scheduler = Scheduler()
    scheduler.add(PeriodicTask("a", lambda: make_job("a"), interval_seconds=0.01))
    scheduler.add(PeriodicTask("b", lambda: make_job("b"), interval_seconds=0.01))
    scheduler.start()
    await asyncio.sleep(0.05)
    await scheduler.stop()
    assert counts["a"] >= 1
    assert counts["b"] >= 1
