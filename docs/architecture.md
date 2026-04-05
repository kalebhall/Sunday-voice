# Sunday Voice – Architecture

## High-Level Design

Sunday Voice consists of:

1. **Web API and UI service (Python + FastAPI)**  
   - Serves operator/admin UI and listener UI.  
   - Exposes REST APIs for session management and settings.  
   - Provides WebSocket endpoints for live text fan-out.

2. **Media pipeline (transcription service)**  
   - Receives audio from operator browser (WebRTC/web audio to server).  
   - Buffers audio briefly.  
   - Streams audio to Whisper API for transcription.  
   - Normalizes transcript segments and sends them to the translation layer.

3. **Translation service**  
   - Wraps one or more translation providers.  
   - Receives transcript segments and fans them out into the enabled target languages.  
   - Pushes translated segments into the WebSocket layer.

4. **TTS service**  
   - Optional per-language TTS synthesis.  
   - Generates audio snippets per segment or sentence.  
   - Exposes URLs or streaming endpoints for clients that want audio.

5. **Persistence layer**  
   - PostgreSQL for:
     - Users, roles.
     - Sessions, configurations.
     - Transcript and translation segments (time-limited).
     - Audit logs.  
   - Redis for:
     - WebSocket pub/sub (if running multiple workers).  
     - Ephemeral session presence and state.

6. **Scheduler / cleanup**  
   - Handles:
     - Session expiration.  
     - 48-hour content deletion.  
     - Budget checks vs thresholds.  

7. **Reverse proxy**  
   - Nginx or Caddy in front of the web service.  
   - TLS termination and static file caching.

## Data Flow (MVP)

1. Operator logs in and starts a session.
2. Browser captures audio (selected input) and sends audio chunks to the server.
3. Server pipeline sends audio to Whisper API; receives streaming transcript segments.
4. Transcript segments are associated with a session and stored (time-limited).
5. Translation service sends segments to configured translation provider and receives translations in requested languages.
6. Translated segments are:
   - Stored (time-limited).
   - Broadcast over WebSockets to connected listeners by language.
7. Listener UIs subscribe over WebSocket to their selected language and render text as it arrives.
8. If TTS is enabled, the TTS service synthesizes audio for segments, and listeners pull or stream it per device.

## Component Boundaries

- **TranscriptionProvider interface**
  - `transcribe_stream(audio_stream, source_language=None) -> segment_stream`
- **TranslationProvider interface**
  - `translate(text, source_language, target_language) -> translated_text`
- **TTSProvider interface**
  - `synthesize(text, language) -> audio_url_or_bytes`
- **CostMeter**
  - `record(provider, operation, units)` and exposes per-period usage.

## Technology Choices (Recommended)

- Backend: Python, FastAPI, asyncio.
- Frontend: Simple React or vanilla JS with minimal dependencies.
- WebSockets: via FastAPI / Starlette.
- Database: PostgreSQL.
- Cache/queue: Redis (optional in single-process but plan for it).
- Deployment: Docker Compose on Ubuntu LXC.
