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

- MVP: Single cloud provider (e.g., Google Translate), configurable.
- Interface allows:
  - Multiple providers.
  - Fallback order.
- Future:
  - Evaluate quality for Samoan/Tagalog.
  - Consider self-hosted models if quality and latency acceptable.

## TTS

- MVP:
  - Start with one provider that covers needed languages.
  - Per-listener, opt-in.
- Future:
  - Local TTS engines.
  - Caching of repeated phrases.
