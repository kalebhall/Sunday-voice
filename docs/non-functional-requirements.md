# Sunday Voice – Non-Functional Requirements

## Reliability

- System must be stable enough for live Sunday meetings.
- Failure modes must be visible to operator (e.g., banners for provider errors, audio loss).
- Brief interruptions (e.g., network hiccups, API timeouts) should not force a full session restart.
- Fallback to Web Speech transcription is available when server-side transcription is unavailable.

## Performance

- Target end-to-end latency: 2–3 seconds from speech to displayed translation.
- Support 3–5 concurrent sessions.
- Typical listener load: ~10 per session; must handle up to 100 in a single session.
- Presentation mode must remain responsive on large displays.

## Scalability

- Single stake instance is primary scope.
- Design should not block future multi-stake usage in architecture, but no strong guarantees needed in MVP.
- Vertical scaling (more CPU/RAM on the single host) is sufficient initially.

## Security

- Local accounts with RBAC for admin and operator.
- No authentication for listeners; they can only access their session’s read-only streams.
- Rate limiting on session join and data endpoints.
- Anti-abuse controls:
  - Session codes hard to guess.
  - Links expire with session.
  - No write paths exposed to anonymous users.

## Privacy

- No support for private/confidential meetings in MVP.
- Audio should be streamed and buffered only as needed, not permanently stored.
- Transcripts/translations retained for max 48 hours, then deleted.
- Long-term stats must not contain personally identifiable content.
- Provider configuration clearly documents data-sharing implications.

## Observability

- Logs:
  - Structured logs for session lifecycle, provider calls, and errors.
- Metrics:
  - Sessions created, active, ended.
  - Listener count per session.
  - Transcription/translation latency and error rates.
  - Provider API usage vs configured budget thresholds.
- Alerts:
  - When usage nears configured cost thresholds.
  - When key providers are failing or unavailable.

## Maintainability

- Clear separation of concerns:
  - Web app / API.
  - Media pipeline.
  - Provider integration.
  - Cleanup and scheduler.
- Documented configuration (e.g., `.env` or YAML).
- Native systemd deployment on Ubuntu LXC (no containers).

## Availability

- Best-effort single-host availability for MVP.
- No HA cluster required initially.
- Must handle typical Sunday use without manual intervention.
