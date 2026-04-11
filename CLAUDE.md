# CLAUDE.md

This file gives Claude Code the context it needs to work on this repository.
Authoritative product, architecture, and policy details live in `docs/`. This
file is intentionally high-level — when specifics change, update the source doc,
not this file.

## What this project is

**Sunday Voice** is a self-hosted, real-time translation web app for in-building
ward and stake meetings. An operator captures audio, the system transcribes and
translates it, and listeners view (and optionally hear) the translated output
on their own devices.

- Primary deployment: single stake instance on an Ubuntu LXC on Proxmox.
- Scale target: 3–5 concurrent sessions, up to ~100 listeners per session.
- Target languages: English, Spanish, Tongan, Tagalog.
- Target latency: 2–3 seconds end-to-end.

## Source of truth

Read the relevant doc before making decisions:

| Topic | File |
|---|---|
| Product scope, roles, functional requirements, acceptance criteria | `docs/product-requirements.md` |
| System architecture, components, data flow, tech choices | `docs/architecture.md` |
| Transcription / translation / TTS provider choices | `docs/provider-strategy.md` |
| Operator UI workflow | `docs/operator-workflow.md` |
| Listener UI workflow | `docs/listener-experience.md` |
| Reliability, performance, security, privacy, observability | `docs/non-functional-requirements.md` |
| Roles, auth, abuse controls, retention, audit logging | `docs/security-and-privacy.md` |
| LXC / Docker Compose deployment | `docs/deployment.md` |
| MVP and post-MVP scope | `docs/roadmap.md` |
| Known unresolved questions | `docs/open-questions.md` |
| Agent behavior and response style | `docs/agents.md` |

## Stack (decided)

- Backend: Python + FastAPI (async), PostgreSQL, Redis.
- Frontend: React + Vite + TypeScript, served as static assets by FastAPI.
- Deployment: native systemd services on Ubuntu LXC (no Docker). Postgres,
  Redis, and reverse proxy installed via apt.
- Reverse proxy: Caddy or Nginx for TLS.
- Transcription: OpenAI Whisper API (MVP); browser Web Speech API as fallback.
- Translation: Google Cloud Translation API v3 (MVP).
- TTS: Google Cloud Text-to-Speech (MVP).

## Working agreements for Claude

- Follow the behavior rules in `docs/agents.md` — be direct, analytical, and
  push back on weak requirements. Privacy-first, data-minimization defaults.
- Respect MVP scope from `docs/roadmap.md`. Do not build post-MVP features
  unless explicitly asked.
- Keep provider access behind the interfaces defined in `docs/architecture.md`
  (TranscriptionProvider, TranslationProvider, TTSProvider, CostMeter).
- Do not create documentation files unless asked. Update existing docs in
  `docs/` when a decision changes them.
- Anonymous listener endpoints are read-only and per-session scoped. No write
  paths for unauthenticated users, ever.
- Audio is streamed/buffered, not stored. Transcripts and translations have a
  48-hour retention cap; only aggregate stats survive beyond that.

## Open decisions

See `docs/open-questions.md`. Operator audio transport supports both chunked
WebSocket uploads (default) and WebRTC-to-server, selectable per session in
the operator console.

## Repository

- GitHub: `kalebhall/sunday-voice`
- Active development branch for this task: `claude/review-docs-setup-DzE4l`
