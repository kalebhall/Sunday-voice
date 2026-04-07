"""Prometheus metrics for Sunday Voice.

All metrics use the ``sunday_`` prefix to avoid collisions with
standard process/Python metrics exposed by prometheus_client.

Intended usage
--------------
Import the metric objects directly and observe/increment them at the
relevant call sites.  The ``/metrics`` HTTP endpoint (registered in
``main.py``) exposes the default CollectorRegistry that prometheus_client
populates automatically.

Metric inventory
----------------
sunday_segment_transcription_duration_seconds
    Histogram — wall-clock time for one Whisper API buffer flush.
    Labels: provider (e.g. "openai").

sunday_segment_translation_duration_seconds
    Histogram — wall-clock time for one translate() call.
    Labels: provider (e.g. "google"), target_language.

sunday_segment_pipeline_duration_seconds
    Histogram — time from TranscriptEvent.published_at to the moment the
    translated segment is sent over the listener WebSocket.  Covers the
    full translation + Redis pub/sub + WS delivery path.

sunday_provider_errors_total
    Counter — total provider-level errors (all retries exhausted).
    Labels: provider, operation (e.g. "transcribe", "translate").

sunday_active_sessions
    Gauge — number of sessions currently in ACTIVE status.
    Seeded from DB on startup; incremented/decremented at start/stop transitions.

sunday_connected_listeners
    Gauge — number of listener WebSocket connections currently open.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

segment_transcription_duration_seconds = Histogram(
    "sunday_segment_transcription_duration_seconds",
    "Wall-clock seconds for one Whisper API buffer flush",
    ["provider"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

segment_translation_duration_seconds = Histogram(
    "sunday_segment_translation_duration_seconds",
    "Wall-clock seconds for one translation provider translate() call",
    ["provider", "target_language"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)

segment_pipeline_duration_seconds = Histogram(
    "sunday_segment_pipeline_duration_seconds",
    "Wall-clock seconds from TranscriptEvent published to listener WebSocket delivery",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

provider_errors_total = Counter(
    "sunday_provider_errors_total",
    "Total provider errors after all retries exhausted",
    ["provider", "operation"],
)

active_sessions = Gauge(
    "sunday_active_sessions",
    "Number of sessions currently in ACTIVE status",
)

connected_listeners = Gauge(
    "sunday_connected_listeners",
    "Number of listener WebSocket connections currently open",
)
