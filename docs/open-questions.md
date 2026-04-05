# Sunday Voice – Open Questions

Remaining unresolved items. Resolved decisions have moved into the relevant
feature docs.

- **Operator audio transport**: chunked MediaRecorder uploads over WebSocket
  vs. WebRTC-to-server (aiortc). Prototype both against the Whisper API before
  committing; Whisper's HTTP API is request/response per chunk, so the WebRTC
  path must justify its complexity with measurable latency or reliability
  wins.
- **Samoan/Tagalog translation quality**: shipping with Google Cloud
  Translation and in-product thumbs-down feedback. Revisit after field data
  shows whether quality is acceptable.
