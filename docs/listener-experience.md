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
  - Listener can scroll back in the current session’s history.
- Presentation mode:
  - Large text version, typically used on TV or projector (opened by operator).

## TTS Playback

- Listener can toggle TTS on/off per device.
- When enabled:
  - Short audio clips play as segments arrive.
- Operator does not control individual listener audio.

## End of Session

- When operator ends the session:
  - Display a clear “Session ended” message.
  - Optionally show summary (e.g., “Thanks for participating”).
- Further text/updates stop.
