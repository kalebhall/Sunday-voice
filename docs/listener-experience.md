# Sunday Voice – Listener Experience

## Joining a Session

Listeners can join in three ways:

- Scan QR code posted in chapel or on screen.
- Navigate to a short URL (e.g., translate.thestand.app) and enter a numeric code.
- Use a direct short URL that encodes both.

If session is not started yet, show a waiting state.

## Selecting Language

- After joining, listener sees:
  - Session title.
  - Available languages based on operator settings.
- Listener picks a language. UI subscribes to that language’s stream.

## Viewing Translations

- Live mode:
  - Text auto-scrolls as new segments arrive.
- Scrollback:
  - Listener can scroll back through the full current-session history
    (unbounded within the session).
- Presentation mode:
  - Large text version, typically used on TV or projector (opened by operator).

## TTS Playback

- Listener can toggle TTS on/off per device.
- When enabled:
  - Short audio clips play as segments arrive.
  - Playback is sequential (FIFO) per listener.
  - If the queue falls behind live, segments older than ~10 seconds are
    skipped so playback catches up to current speech rather than drifting.
- Operator does not control individual listener audio.

## Translation Quality Feedback

- Listeners (and operator) can flag a segment's translation as bad (thumbs
  down) directly in the UI.
- Feedback is stored as anonymized metadata (session id, target language,
  segment id, timestamp) and survives the 48-hour content purge so admins can
  review quality trends, especially for Tongan and Tagalog.

## Supported Browsers

Listener UI targets modern evergreen browsers:

- iOS Safari 15+
- Chrome / Chromium / Edge 108+
- Firefox 110+
- Android Chrome 108+

Older browsers may work but are not tested or supported.

## End of Session

- When operator ends the session:
  - Display a clear “Session ended” message.
  - Optionally show summary (e.g., “Thanks for participating”).
- Further text/updates stop.
