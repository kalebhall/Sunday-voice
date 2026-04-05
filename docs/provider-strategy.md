# Sunday Voice – Provider Strategy

## Transcription

- **MVP default**: OpenAI Whisper API
  - Pros: supports English, Spanish, Samoan, Tagalog; good accuracy; no GPU required.
  - Cons: audio leaves your server; cost per minute.

- **Future option**: Self-hosted Whisper
  - Runs locally on GPU/CPU.
  - Eliminates external audio sharing.
  - More ops complexity.

- **Backup**: Browser Web Speech API
  - Use only when server-side pipeline fails.
  - Limited browser support and language coverage.

## Translation

- **MVP default**: Google Cloud Translation API (v3).
  - Pros: broadest coverage including Samoan and Tagalog; single vendor billing
    alongside TTS; predictable latency; mature client libraries.
  - Cons: text leaves your server; per-character cost.
- Interface allows:
  - Multiple providers.
  - Fallback order.
- Future:
  - Evaluate quality for Samoan/Tagalog.
  - Consider self-hosted models if quality and latency acceptable.

## TTS

- **MVP default**: Google Cloud Text-to-Speech.
  - Pros: covers all four target languages; same vendor as translation; decent
    voice quality; supports SSML and MP3/OGG output.
  - Cons: per-character cost; cloud dependency.
- Per-listener, opt-in.
- Future:
  - Local TTS engines.
  - Caching of repeated phrases.
