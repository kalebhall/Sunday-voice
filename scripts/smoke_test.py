#!/usr/bin/env python3
"""Sunday Voice end-to-end smoke test.

Creates a session, streams a WAV file over the operator audio WebSocket, and
asserts that at least one translated segment arrives on a listener WebSocket
within the configured timeout.

Product latency target: ≤3 s end-to-end.  The default --timeout is 30 s to
accommodate Whisper API round-trips during a full pipeline test.  Pass
``--timeout 3`` to enforce the product target strictly.

Dependencies (install alongside the backend venv, or separately)::

    pip install httpx "websockets>=12"

Quick-start::

    python scripts/smoke_test.py \\
        --email operator@example.com \\
        --password s3cret \\
        --wav /path/to/speech.wav

For connectivity testing without a speech WAV::

    python scripts/smoke_test.py \\
        --email operator@example.com \\
        --password s3cret \\
        --generate-wav

Note: ``--generate-wav`` produces a sine-wave tone; Whisper may return empty
text, so no translated segment will arrive.  Use a real speech WAV for a
meaningful full-pipeline test.

Exit codes
----------
0   All assertions passed (segment arrived within ``--timeout``).
1   Assertion failed, unexpected error, or bad arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import struct
import sys
import time
import wave
from pathlib import Path
from typing import Any

try:
    import httpx
    import websockets
    import websockets.asyncio.client as ws_client
except ImportError as exc:
    print(
        f"Missing dependency: {exc}\n"
        "Install with: pip install httpx 'websockets>=12'",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRODUCT_LATENCY_TARGET_S = 3.0

# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------


def _generate_wav(duration_s: float = 4.0, sample_rate: int = 16_000) -> bytes:
    """Return in-memory WAV bytes: a 440 Hz sine wave for connectivity testing."""
    n_samples = int(sample_rate * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            sample = int(32_767 * math.sin(2 * math.pi * 440 * i / sample_rate))
            frames += struct.pack("<h", sample)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def _load_wav(path: str) -> bytes:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"WAV file not found: {path}")
    return p.read_bytes()


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _http_base(base_url: str) -> str:
    return base_url.rstrip("/")


def _ws_base(base_url: str) -> str:
    http = base_url.rstrip("/")
    if http.startswith("https://"):
        return "wss://" + http[len("https://"):]
    return "ws://" + http[len("http://"):]


# ---------------------------------------------------------------------------
# HTTP calls
# ---------------------------------------------------------------------------


async def login(
    http: httpx.AsyncClient, base_url: str, email: str, password: str
) -> str:
    """Authenticate and return an access token."""
    resp = await http.post(
        f"{_http_base(base_url)}/api/auth/login",
        json={"email": email, "password": password},
    )
    resp.raise_for_status()
    token: str = resp.json()["access_token"]
    _log("login", f"authenticated as {email}")
    return token


async def create_session(
    http: httpx.AsyncClient,
    base_url: str,
    token: str,
    source_lang: str,
    target_lang: str,
) -> dict[str, Any]:
    """Create a DRAFT session with one target language."""
    resp = await http.post(
        f"{_http_base(base_url)}/api/sessions",
        json={
            "name": "smoke-test",
            "source_language": source_lang,
            "audio_transport": "websocket_chunks",
            "target_languages": [{"language_code": target_lang, "tts_enabled": False}],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    session: dict[str, Any] = resp.json()
    _log("session", f"created  id={session['id']}  join_code={session['join_code']}")
    return session


async def start_session(
    http: httpx.AsyncClient, base_url: str, token: str, session_id: str
) -> None:
    resp = await http.post(
        f"{_http_base(base_url)}/api/sessions/{session_id}/start",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    _log("session", f"started  id={session_id}")


async def stop_session(
    http: httpx.AsyncClient, base_url: str, token: str, session_id: str
) -> None:
    resp = await http.post(
        f"{_http_base(base_url)}/api/sessions/{session_id}/stop",
        headers={"Authorization": f"Bearer {token}"},
    )
    # 409 = already ended; treat as success.
    if resp.status_code not in (200, 409):
        resp.raise_for_status()
    _log("session", f"stopped  id={session_id}")


# ---------------------------------------------------------------------------
# WebSocket tasks
# ---------------------------------------------------------------------------


async def listener_task(
    ws_base: str,
    join_code: str,
    target_lang: str,
    segment_received: asyncio.Event,
    segment_info: dict[str, Any],
    deadline: float,
) -> None:
    """Connect as an anonymous listener and set *segment_received* on first segment."""
    url = f"{ws_base}/ws/listen/{join_code}/{target_lang}"
    try:
        async with ws_client.connect(url) as ws:
            _log("listener", f"connected  url={url}")
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                msg_type = msg.get("type")
                if msg_type == "scrollback":
                    _log("listener", f"scrollback ({msg.get('count', 0)} prior segments)")
                elif msg_type == "heartbeat":
                    pass  # keepalive; ignore
                elif msg_type == "segment":
                    segment_info.update(
                        text=msg.get("text", ""),
                        seq=msg.get("seq"),
                        language=msg.get("language"),
                    )
                    segment_received.set()
                    _log(
                        "listener",
                        f"segment  seq={msg.get('seq')}  lang={msg.get('language')}"
                        f"  text={msg.get('text', '')!r}",
                    )
                    return
                elif msg_type == "session_ended":
                    _log("listener", "session_ended signal received")
                    return
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        _log("listener", f"error: {exc}")


async def operator_task(
    ws_base: str,
    session_id: str,
    token: str,
    audio_bytes: bytes,
    chunk_size: int,
    timing: dict[str, float],
) -> None:
    """Stream *audio_bytes* to the operator audio WebSocket in fixed-size chunks."""
    url = f"{ws_base}/ws/operator/{session_id}/audio?token={token}"
    n_chunks = math.ceil(len(audio_bytes) / chunk_size)
    async with ws_client.connect(url) as ws:
        _log("operator", f"connected  chunks≈{n_chunks}  total={len(audio_bytes):,} B")
        offset = 0
        first = True
        while offset < len(audio_bytes):
            chunk = audio_bytes[offset : offset + chunk_size]
            offset += chunk_size
            await ws.send(chunk)
            if first:
                timing["t0"] = time.monotonic()
                first = False
            # Yield between chunks so the listener task can process messages.
            await asyncio.sleep(0)
        _log("operator", "all audio sent; closing")
    timing["t_disconnect"] = time.monotonic()


# ---------------------------------------------------------------------------
# Main test coroutine
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    """Execute the smoke test.  Returns 0 on pass, 1 on fail."""

    # --- Prepare audio -------------------------------------------------------
    if args.generate_wav:
        _log("audio", "generating synthetic 4 s 440 Hz WAV (connectivity test only)")
        audio_bytes = _generate_wav()
    else:
        _log("audio", f"loading {args.wav}")
        try:
            audio_bytes = _load_wav(args.wav)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    _log("audio", f"{len(audio_bytes):,} bytes ready")

    ws_base = _ws_base(args.base_url)
    session_id: str | None = None

    async with httpx.AsyncClient(timeout=30.0) as http:
        # 1. Authenticate
        try:
            token = await login(http, args.base_url, args.email, args.password)
        except httpx.HTTPStatusError as exc:
            print(f"ERROR: login failed ({exc.response.status_code})", file=sys.stderr)
            return 1

        # 2. Create and start session
        try:
            session = await create_session(
                http, args.base_url, token, args.source_lang, args.target_lang
            )
        except httpx.HTTPStatusError as exc:
            print(f"ERROR: session create failed ({exc.response.status_code})", file=sys.stderr)
            return 1

        session_id = session["id"]
        join_code = session["join_code"]

        try:
            await start_session(http, args.base_url, token, session_id)
        except httpx.HTTPStatusError as exc:
            print(f"ERROR: session start failed ({exc.response.status_code})", file=sys.stderr)
            await _cleanup(http, args.base_url, token, session_id, args.keep_session)
            return 1

        # 3. Start listener task *before* streaming so no segment is missed
        segment_received = asyncio.Event()
        segment_info: dict[str, Any] = {}
        timing: dict[str, float] = {}
        deadline = time.monotonic() + args.timeout + 10  # generous listener deadline

        listener = asyncio.create_task(
            listener_task(
                ws_base, join_code, args.target_lang,
                segment_received, segment_info, deadline,
            ),
            name="listener",
        )

        # Allow the listener WS to subscribe before audio starts flowing.
        await asyncio.sleep(0.5)

        # 4. Stream audio as operator
        try:
            await operator_task(
                ws_base, session_id, token,
                audio_bytes, args.chunk_bytes, timing,
            )
        except Exception as exc:
            print(f"ERROR: operator stream failed: {exc}", file=sys.stderr)
            listener.cancel()
            await _cleanup(http, args.base_url, token, session_id, args.keep_session)
            return 1

        # 5. Wait for translated segment
        t0 = timing.get("t0", time.monotonic())
        elapsed_so_far = time.monotonic() - t0
        remaining = max(args.timeout - elapsed_so_far, 1.0)
        _log("wait", f"operator disconnected; waiting up to {remaining:.1f} s for segment…")

        try:
            await asyncio.wait_for(segment_received.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            listener.cancel()
            print(
                f"\nFAIL: no translated segment arrived within {args.timeout:.1f} s "
                f"of first audio byte.\n"
                "     Check: OPENAI_API_KEY is valid; Google credentials are set; "
                "the WAV contains speech.",
                file=sys.stderr,
            )
            await _cleanup(http, args.base_url, token, session_id, args.keep_session)
            return 1

        listener.cancel()
        elapsed = time.monotonic() - t0

        # 6. Report latency
        print()
        _log("result", f"segment arrived in {elapsed:.2f} s")
        _log("result", f"text     : {segment_info.get('text', '')!r}")
        _log("result", f"language : {segment_info.get('language')}")
        _log("result", f"seq      : {segment_info.get('seq')}")
        print()

        if elapsed <= PRODUCT_LATENCY_TARGET_S:
            print(f"PASS  latency {elapsed:.2f} s ≤ {PRODUCT_LATENCY_TARGET_S} s product target")
        else:
            print(
                f"PASS  segment received ({elapsed:.2f} s — exceeds {PRODUCT_LATENCY_TARGET_S} s "
                f"product target; consider profiling the Whisper/Translation round-trips)"
            )

        # 7. Clean up
        await _cleanup(http, args.base_url, token, session_id, args.keep_session)

    return 0


async def _cleanup(
    http: httpx.AsyncClient,
    base_url: str,
    token: str,
    session_id: str | None,
    keep_session: bool,
) -> None:
    if keep_session or session_id is None:
        return
    try:
        await stop_session(http, base_url, token, session_id)
    except Exception as exc:
        _log("cleanup", f"warning: could not stop session: {exc}")


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------


def _log(component: str, message: str) -> None:
    print(f"[{component:<9}] {message}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke_test.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        metavar="URL",
        help="Base URL of the running Sunday Voice instance (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Operator or admin account e-mail address",
    )
    parser.add_argument(
        "--password",
        required=True,
        help="Account password",
    )

    audio_group = parser.add_mutually_exclusive_group(required=True)
    audio_group.add_argument(
        "--wav",
        metavar="PATH",
        help="Path to a WAV file to stream as operator audio",
    )
    audio_group.add_argument(
        "--generate-wav",
        action="store_true",
        help=(
            "Generate a synthetic 4 s 440 Hz sine-wave WAV for connectivity testing. "
            "Whisper typically returns empty text for a pure tone; "
            "use a real speech WAV for a meaningful full-pipeline test."
        ),
    )

    parser.add_argument(
        "--source-lang",
        default="en",
        metavar="CODE",
        help="Source language code (default: en)",
    )
    parser.add_argument(
        "--target-lang",
        default="es",
        metavar="CODE",
        help="Target translation language code (default: es)",
    )
    parser.add_argument(
        "--chunk-bytes",
        type=int,
        default=32_768,
        metavar="N",
        help=(
            "Bytes per audio frame sent to the operator WebSocket (default: 32768 = 32 KB). "
            "Matches a ~1 s WebM/Opus chunk at 256 kbps."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECS",
        help=(
            "Seconds from first audio byte to wait for a translated segment (default: 30). "
            f"The product target is {PRODUCT_LATENCY_TARGET_S} s; "
            f"pass --timeout {PRODUCT_LATENCY_TARGET_S:.0f} to enforce it strictly."
        ),
    )
    parser.add_argument(
        "--keep-session",
        action="store_true",
        help="Do not stop the test session on exit (useful for debugging)",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
