# Sunday Voice – Product Requirements

## Overview

Sunday Voice is a self-hosted, real-time translation web app for in-building ward and stake meetings. An operator captures audio from a meeting, the system transcribes and translates it, and listeners view or hear the translated output on their own devices.

The first deployment is a single stake instance on a personal Ubuntu server (LXC on Proxmox), supporting 3–5 concurrent sessions with 1–100 listeners per session.

## Goals

- Support ward and stake meetings first, and be usable for other in-building meetings.
- Provide near real-time transcription and translation with roughly 2–3 seconds of end-to-end latency.
- Support English, Spanish, Samoan, and Tagalog, including multiple target languages simultaneously in one session.
- Provide both text display and optional spoken playback (TTS) per listener.
- Allow anonymous listener access using expiring links / codes (no login).
- Minimize data retention: delete audio and transcript content after 48 hours while keeping statistical data.
- Deliver high enough reliability to be trusted in live Church meetings.
- Keep operating costs low and support budget alert thresholds for external APIs.

## Non-Goals (MVP)

- Private/confidential meetings (interviews, councils, etc.).
- Out-of-building streaming workflows (Zoom, YouTube, etc.).
- Permanent archival of audio or transcripts.
- Full integration with Church Account or official calendars in v1.

## Roles and Users

### Admin

- Has a local account with admin role.
- Manages operator accounts and roles.
- Configures transcription, translation, and TTS providers and keys.
- Sets retention, budget thresholds, and global system settings.

### Operator

- Has a local account with operator role.
- Creates and manages live translation sessions (ad hoc or pre-scheduled).
- Selects audio input and source language (or chooses auto-detect).
- Monitors health and latency indicators.
- Can override language detection, pause, mute, and stop sessions.
- Uses presentation mode for closed-caption-like display.

### Listener

- Anonymous user joining by QR code, short URL, or numeric code.
- Reads translated text and can scroll back during the session.
- Optionally enables TTS audio playback on their device.
- Chooses which language stream to see/hear among operator-enabled languages.

## Core Use Cases

1. **Sacrament Meeting Translation**
   - Operator at sound desk captures chapel audio.
   - Sunday Voice provides live translated text in Spanish and Samoan on member phones and in an overflow room presentation mode.

2. **Stake Conference**
   - Multiple sessions (e.g., adult session, general session) configured and pre-scheduled.
   - More listeners (up to ~100) join simultaneously per session.

3. **Classroom / Auxiliary Meetings**
   - Smaller sessions created ad hoc.
   - Text-only translation often sufficient.

## Functional Requirements

### Session Management

- Create sessions ad hoc or from pre-scheduled entries.
- Assign a name, scheduled start/end, and enabled languages.
- Generate join info: QR code, short URL, and numeric session code.
- Enforce automatic expiration: sessions cannot be active longer than 12 hours.
- Support 3–5 concurrent active sessions on the same stake instance.
- Show active/ended/pending sessions in operator/admin UI.

### Listener Access

- Allow anonymous access; no login required.
- Join via:
  - Scanning QR code.
  - Visiting short URL and entering a code.
  - Direct short URL that encodes session id/code.
- Limit access to existing, non-expired sessions.
- Show clear state: waiting for session to start, live, ended.

### Transcription

- Primary: server-side transcription via Whisper API.
- Backup: browser Web Speech API used only when server pipeline is unavailable.
- Support audio buffering for a short window to improve robustness.
- Automatically detect spoken language by default.
- Allow operator to manually select language when auto-detect is incorrect.
- Provide indication in operator UI when transcription is degraded or failing.

### Translation

- Translate from detected/source language into:
  - English
  - Spanish
  - Samoan
  - Tagalog
- Support multiple simultaneous target languages in the same session.
- Allow operator to enable/disable which target languages are exposed per session.
- Abstraction for translation providers to support swapping (Google, others).

### Display and TTS

- Provide responsive web UI for phone, tablet, laptop.
- Allow listeners to:
  - See ongoing translated text (auto-scroll).
  - Scroll back through current session transcript.
  - Choose language from available options.
  - Enable/disable TTS audio playback per device.
- Provide operator-controlled presentation/closed-caption mode:
  - Large text, high contrast.
  - Works well on TV/projector.

### Data and Analytics

- Store transcripts and translations for up to 48 hours after session end.
- After 48 hours, delete all content; keep long-term aggregates:
  - Session counts and durations.
  - Listener counts per session.
  - Language usage.
  - Provider usage and cost metrics.
- Provide basic dashboard for admin:
  - Recent sessions.
  - API usage vs budget thresholds.
  - Error rates.

## Acceptance Criteria (MVP)

- Operator can create a session, select input, and start/stop it.
- Listeners can join anonymously and see live translated text with acceptable latency.
- Multiple target languages in the same session are supported.
- Presentation/closed-caption mode works from an operator browser.
- Data older than 48 hours is automatically removed (except aggregate statistics).
- System can handle 3 concurrent sessions and ~50 listeners total without failing.
