"""Tests for the in-process asyncio transcript pub/sub."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.services.pubsub import SessionPubSub, TranscriptEvent, TranscriptPubSubRegistry


def _make_event(session_id=None, seq=1, text="hello") -> TranscriptEvent:
    return TranscriptEvent(
        session_id=session_id or uuid4(),
        sequence=seq,
        language="en",
        text=text,
    )


class TestSessionPubSub:
    async def test_subscribe_and_publish(self) -> None:
        ps = SessionPubSub()
        sub_id, q = await ps.subscribe()
        event = _make_event()
        await ps.publish(event)
        received = q.get_nowait()
        assert received is event
        await ps.unsubscribe(sub_id)

    async def test_fan_out_to_multiple_subscribers(self) -> None:
        ps = SessionPubSub()
        _, q1 = await ps.subscribe()
        _, q2 = await ps.subscribe()
        event = _make_event()
        await ps.publish(event)
        assert q1.get_nowait() is event
        assert q2.get_nowait() is event

    async def test_backpressure_drops_oldest(self) -> None:
        ps = SessionPubSub(maxsize=2)
        _, q = await ps.subscribe()
        e1 = _make_event(seq=1, text="first")
        e2 = _make_event(seq=2, text="second")
        e3 = _make_event(seq=3, text="third")
        await ps.publish(e1)
        await ps.publish(e2)
        # Queue is now full (maxsize=2).  Publishing e3 should evict e1.
        await ps.publish(e3)
        got1 = q.get_nowait()
        got2 = q.get_nowait()
        assert got1.text == "second"
        assert got2.text == "third"

    async def test_unsubscribe_removes_subscriber(self) -> None:
        ps = SessionPubSub()
        sub_id, q = await ps.subscribe()
        await ps.unsubscribe(sub_id)
        assert ps.subscriber_count == 0
        # Publishing after unsubscribe should not raise.
        await ps.publish(_make_event())

    async def test_subscriber_count(self) -> None:
        ps = SessionPubSub()
        assert ps.subscriber_count == 0
        id1, _ = await ps.subscribe()
        id2, _ = await ps.subscribe()
        assert ps.subscriber_count == 2
        await ps.unsubscribe(id1)
        assert ps.subscriber_count == 1
        await ps.unsubscribe(id2)
        assert ps.subscriber_count == 0


class TestTranscriptPubSubRegistry:
    async def test_get_or_create_reuses_instance(self) -> None:
        reg = TranscriptPubSubRegistry()
        sid = uuid4()
        ps1 = await reg.get_or_create(sid)
        ps2 = await reg.get_or_create(sid)
        assert ps1 is ps2

    async def test_publish_to_existing_session(self) -> None:
        reg = TranscriptPubSubRegistry()
        sid = uuid4()
        ps = await reg.get_or_create(sid)
        _, q = await ps.subscribe()
        event = _make_event(session_id=sid)
        await reg.publish(event)
        assert q.get_nowait() is event

    async def test_publish_to_unknown_session_is_noop(self) -> None:
        reg = TranscriptPubSubRegistry()
        # Should not raise.
        await reg.publish(_make_event(session_id=uuid4()))

    async def test_remove_if_empty(self) -> None:
        reg = TranscriptPubSubRegistry()
        sid = uuid4()
        ps = await reg.get_or_create(sid)
        assert sid in reg.active_sessions
        # No subscribers, so remove_if_empty should clean up.
        await reg.remove_if_empty(sid)
        assert sid not in reg.active_sessions

    async def test_remove_if_empty_keeps_non_empty(self) -> None:
        reg = TranscriptPubSubRegistry()
        sid = uuid4()
        ps = await reg.get_or_create(sid)
        sub_id, _ = await ps.subscribe()
        await reg.remove_if_empty(sid)
        # Still has a subscriber, should not be removed.
        assert sid in reg.active_sessions
        await ps.unsubscribe(sub_id)
        await reg.remove_if_empty(sid)
        assert sid not in reg.active_sessions
