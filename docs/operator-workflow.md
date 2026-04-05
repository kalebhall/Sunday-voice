# Sunday Voice – Operator Workflow

## Before the Meeting

1. Log in as operator.
2. Check server health indicators.
3. Confirm provider configuration (Whisper API, translation provider).
4. Verify audio input options on the operator device.

## Creating a Session

- Option A: Pre-scheduled
  - Admin/operator creates a scheduled session with:
    - Name (e.g., "Ward Sacrament – 9 AM").
    - Start time and expected end time.
    - Enabled target languages.
  - System generates join link, QR code, and numeric code.

- Option B: Ad hoc
  - Operator creates a new session on the fly with minimal fields.
  - Session becomes available immediately.

## Starting the Session

1. Open the operator console for the session.
2. Select audio input (sound system, external mic, etc.).
3. Choose audio transport:
   - **Chunked WebSocket (default)**: reliable, ~2–3s chunks of WebM/Opus
     uploaded from the browser.
   - **WebRTC**: continuous audio track to the server via aiortc; try this
     if chunked uploads feel laggy or drop audio.
4. Choose transcription mode:
   - Default: server-side (Whisper API).
   - Backup: browser Web Speech (only if server mode unavailable).
5. Source language:
   - Auto-detect by default.
   - Manual override if needed.
6. Start session:
   - System begins streaming audio, receiving transcripts, and sending translations.

## During the Meeting

- Monitor:
  - Latency indicators.
  - Error/warning banners (audio lost, provider failover).
  - Listener count.
- Controls:
  - Pause/resume output.
  - Mute translated output temporarily.
  - Manually switch source language if auto-detect failing.
- Presentation mode:
  - Open presentation view on a second screen/monitor for closed-caption style display.

## Handling Errors

- Audio lost:
  - System shows "audio lost" banner.
  - Operator checks cabling/inputs; may switch input source.
- Provider failure:
  - System shows "transcription/translation unavailable".
  - Operator can switch to Web Speech fallback if supported.
- Network issues:
  - If operator disconnects, system detects and pauses.

## Ending the Session

1. Stop transcription and translation.
2. Confirm session end in UI (prevents further joins).
3. System keeps transcripts for 48 hours and then deletes them.
4. Operator can view basic stats about the session.
