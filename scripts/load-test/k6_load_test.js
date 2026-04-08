/**
 * Sunday Voice — k6 load test
 *
 * Simulates 5 concurrent sessions, each with 100 listener WebSocket
 * connections and one operator injecting transcripts (or streaming binary
 * audio) every 2.5 seconds.
 *
 * Primary metric:  e2e_latency_ms — time from TranscriptEvent creation
 *                  (post-transcription) to the listener receiving the
 *                  translated segment.
 * Pass threshold:  p(95) < 3 000 ms
 *
 * Usage
 * -----
 *   # Transcript-injection mode (no Whisper API, no audio file needed):
 *   k6 run \
 *     --env BASE_URL=http://localhost:8000 \
 *     --env ADMIN_EMAIL=admin@example.com \
 *     --env ADMIN_PASSWORD=Sunday1234! \
 *     scripts/load-test/k6_load_test.js
 *
 *   # Full audio-pipeline mode (requires OPENAI_API_KEY on the server and
 *   # a pre-generated silent_chunk.webm in the same directory):
 *   bash scripts/load-test/gen_silent_chunk.sh
 *   k6 run --env USE_AUDIO=1 ... scripts/load-test/k6_load_test.js
 *
 * Environment variables
 * ---------------------
 *   BASE_URL               Server base URL           (default: http://localhost:8000)
 *   ADMIN_EMAIL            Admin/operator email      (default: admin@example.com)
 *   ADMIN_PASSWORD         Admin/operator password   (default: Sunday1234!)
 *   NUM_SESSIONS           Sessions to create        (default: 5)
 *   LISTENERS_PER_SESSION  Listener VUs per session  (default: 100)
 *   TARGET_LANGS           Comma-separated languages (default: es,sm,tl)
 *   INJECT_INTERVAL_MS     Operator send cadence ms  (default: 2500)
 *   USE_AUDIO              1 = send binary audio chunks, 0 = inject text
 *                          (default: 0)
 *   RAMP_UP_SECONDS        Listener ramp-up duration (default: 30)
 *   HOLD_SECONDS           Hold-load duration        (default: 120)
 */

import http from "k6/http";
import ws from "k6/ws";
import { check, sleep } from "k6";
import { Trend, Counter, Gauge, Rate } from "k6/metrics";

// ---------------------------------------------------------------------------
// Custom metrics
// ---------------------------------------------------------------------------

/** Wall-clock ms from TranscriptEvent.published_at to listener receive.
 *  Measures: translation + Redis pub/sub + WebSocket delivery. */
const e2eLatency = new Trend("e2e_latency_ms", true);

/** Number of translated segments received by listeners. */
const segmentsReceived = new Counter("segments_received");

/** Number of transcript frames sent by operators. */
const operatorSends = new Counter("operator_sends");

/** Active listener WebSocket connections at any point. */
const activeListeners = new Gauge("active_listeners");

/** Rate of WebSocket connection failures. */
const wsConnectErrors = new Rate("ws_connect_errors");

// ---------------------------------------------------------------------------
// Configuration from environment
// ---------------------------------------------------------------------------

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const WS_URL = BASE_URL.replace(/^http/, "ws");
const ADMIN_EMAIL = __ENV.ADMIN_EMAIL || "admin@example.com";
const ADMIN_PASSWORD = __ENV.ADMIN_PASSWORD || "Sunday1234!";
const NUM_SESSIONS = parseInt(__ENV.NUM_SESSIONS || "5", 10);
const LISTENERS_PER_SESSION = parseInt(__ENV.LISTENERS_PER_SESSION || "100", 10);
const TARGET_LANGS = (__ENV.TARGET_LANGS || "es,sm,tl").split(",");
const INJECT_INTERVAL_MS = parseInt(__ENV.INJECT_INTERVAL_MS || "2500", 10);
const USE_AUDIO = __ENV.USE_AUDIO === "1";
const RAMP_UP_SECONDS = parseInt(__ENV.RAMP_UP_SECONDS || "30", 10);
const HOLD_SECONDS = parseInt(__ENV.HOLD_SECONDS || "120", 10);

const TOTAL_LISTENERS = NUM_SESSIONS * LISTENERS_PER_SESSION;

// Max test duration: operators run for the full window; listeners ramp up,
// hold, then ramp back down.
const OPERATOR_DURATION_S = RAMP_UP_SECONDS + HOLD_SECONDS + 30; // +30s buffer
const TOTAL_DURATION_S = OPERATOR_DURATION_S + 30;

// ---------------------------------------------------------------------------
// Static audio chunk (USE_AUDIO=1 only)
// ---------------------------------------------------------------------------

// Loaded once at init time.  Only used when USE_AUDIO=1.
// Run `bash scripts/load-test/gen_silent_chunk.sh` to generate it.
let silentChunk;
if (USE_AUDIO) {
  silentChunk = open("silent_chunk.webm", "b");
}

// ---------------------------------------------------------------------------
// Operator transcript texts — short sentences looped in order to simulate
// realistic speech cadence.
// ---------------------------------------------------------------------------

const TRANSCRIPT_TEXTS = [
  "Brothers and sisters, welcome to our sacrament meeting.",
  "We will now hear from our first speaker.",
  "The topic today is gratitude and thankfulness.",
  "Let us reflect on the blessings we have received this week.",
  "The Spirit of the Lord is with us today.",
  "We are grateful for the opportunity to worship together.",
  "Please open your hymnals to page one fifty seven.",
  "The closing prayer will be offered by Sister Martinez.",
  "We invite all members to stay for the second hour.",
  "Thank you for your faithful attendance and participation.",
];

// ---------------------------------------------------------------------------
// k6 scenario options
// ---------------------------------------------------------------------------

export const options = {
  scenarios: {
    // One operator VU per session, running the full duration.
    operators: {
      executor: "constant-vus",
      vus: NUM_SESSIONS,
      duration: `${OPERATOR_DURATION_S}s`,
      exec: "operatorScenario",
      tags: { role: "operator" },
    },
    // Listeners ramp up to peak then back down.
    // Start 5 s after operators so sessions are fully active.
    listeners: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: `${RAMP_UP_SECONDS}s`, target: TOTAL_LISTENERS },
        { duration: `${HOLD_SECONDS}s`, target: TOTAL_LISTENERS },
        { duration: "30s", target: 0 },
      ],
      exec: "listenerScenario",
      startTime: "5s",
      tags: { role: "listener" },
    },
  },

  thresholds: {
    // PRIMARY: p95 end-to-end latency (translation + Redis + WS delivery)
    // must stay under 3 s.  Note: Whisper transcription time is upstream of
    // published_at; see the server-side `segment_transcription_duration_seconds`
    // Prometheus metric for the full pipeline breakdown.
    e2e_latency_ms: ["p(95)<3000"],

    // WebSocket connect errors must stay below 1 %.
    ws_connect_errors: ["rate<0.01"],

    // At least one segment should arrive during the test (smoke check).
    segments_received: ["count>0"],

    // HTTP requests used in setup should not fail.
    "http_req_failed{scenario:setup}": ["rate<0.01"],
  },
};

// ---------------------------------------------------------------------------
// Setup — runs once; creates sessions and returns shared state
// ---------------------------------------------------------------------------

export function setup() {
  // Login
  const loginRes = http.post(
    `${BASE_URL}/api/auth/login`,
    JSON.stringify({ email: ADMIN_EMAIL, password: ADMIN_PASSWORD }),
    { headers: { "Content-Type": "application/json" }, tags: { scenario: "setup" } }
  );
  check(loginRes, { "login 200": (r) => r.status === 200 });
  if (loginRes.status !== 200) {
    console.error(`Login failed: ${loginRes.status} ${loginRes.body}`);
    return { sessions: [], baseUrl: BASE_URL };
  }
  const { access_token: token } = loginRes.json();

  const sessions = [];

  for (let i = 0; i < NUM_SESSIONS; i++) {
    // Create session
    const createRes = http.post(
      `${BASE_URL}/api/sessions`,
      JSON.stringify({
        name: `load-test-session-${i + 1}`,
        source_language: "en",
        // Use web_speech transport for transcript-injection mode so we don't
        // need a Whisper API key; use websocket_chunks for audio mode.
        audio_transport: USE_AUDIO ? "websocket_chunks" : "web_speech",
        target_languages: TARGET_LANGS.map((l) => ({
          language_code: l,
          tts_enabled: false,
        })),
      }),
      {
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        tags: { scenario: "setup" },
      }
    );
    check(createRes, { "session created 201": (r) => r.status === 201 });
    if (createRes.status !== 201) {
      console.error(`Session create failed: ${createRes.status} ${createRes.body}`);
      continue;
    }
    const session = createRes.json();

    // Start session (draft → active)
    const startRes = http.post(
      `${BASE_URL}/api/sessions/${session.id}/start`,
      null,
      {
        headers: { Authorization: `Bearer ${token}` },
        tags: { scenario: "setup" },
      }
    );
    check(startRes, { "session started 200": (r) => r.status === 200 });

    sessions.push({
      id: session.id,
      join_code: session.join_code,
      token,
    });
    console.log(
      `Session ${i + 1}: id=${session.id} join_code=${session.join_code}`
    );
  }

  return { sessions, baseUrl: BASE_URL };
}

// ---------------------------------------------------------------------------
// Operator scenario — sends transcripts or audio chunks every 2.5 s
// ---------------------------------------------------------------------------

export function operatorScenario(data) {
  if (!data.sessions || data.sessions.length === 0) {
    console.error("No sessions available; operator aborting");
    return;
  }

  // Each operator VU is pinned to one session by VU index.
  const sessionIdx = (__VU - 1) % data.sessions.length;
  const session = data.sessions[sessionIdx];

  const endpoint = USE_AUDIO
    ? `${WS_URL}/ws/operator/${session.id}/audio?token=${session.token}`
    : `${WS_URL}/ws/operator/${session.id}/transcript?token=${session.token}`;

  let msgIndex = 0;

  const res = ws.connect(
    endpoint,
    { tags: { role: "operator" } },
    (socket) => {
      socket.on("open", () => {
        // Send the first frame immediately.
        sendOperatorFrame(socket, msgIndex++);

        // Then send on a fixed cadence.
        socket.setInterval(() => {
          sendOperatorFrame(socket, msgIndex++);
        }, INJECT_INTERVAL_MS);
      });

      socket.on("error", (e) => {
        console.error(`operator[${sessionIdx}] WS error: ${e.error}`);
      });

      socket.on("close", (code, reason) => {
        if (code !== 1000 && code !== 1001) {
          console.warn(`operator[${sessionIdx}] closed: code=${code} reason=${reason}`);
        }
      });

      // Keep the socket alive for the scenario duration.
      socket.setTimeout(() => socket.close(), OPERATOR_DURATION_S * 1000);
    }
  );

  wsConnectErrors.add(!(res && res.status === 101));
  check(res, { "operator WS 101": (r) => r && r.status === 101 });
}

function sendOperatorFrame(socket, index) {
  if (USE_AUDIO) {
    // Binary WebM/Opus chunk (pre-generated silent audio).
    socket.sendBinary(silentChunk);
  } else {
    // JSON text frame for the Web Speech / transcript-injection path.
    const text = TRANSCRIPT_TEXTS[index % TRANSCRIPT_TEXTS.length];
    socket.send(JSON.stringify({ text, language: "en" }));
  }
  operatorSends.add(1);
}

// ---------------------------------------------------------------------------
// Listener scenario — connects and records per-segment latency
// ---------------------------------------------------------------------------

export function listenerScenario(data) {
  if (!data.sessions || data.sessions.length === 0) {
    console.error("No sessions available; listener aborting");
    return;
  }

  // Distribute listeners evenly across sessions and languages.
  const vuIndex = __VU - 1;
  const sessionIdx = vuIndex % data.sessions.length;
  const langIdx = Math.floor(vuIndex / data.sessions.length) % TARGET_LANGS.length;
  const session = data.sessions[sessionIdx];
  const lang = TARGET_LANGS[langIdx];

  const wsUrl = `${WS_URL}/ws/listen/${session.join_code}/${lang}`;

  const res = ws.connect(
    wsUrl,
    { tags: { role: "listener", lang } },
    (socket) => {
      activeListeners.add(1);

      socket.on("message", (msg) => {
        let payload;
        try {
          payload = JSON.parse(msg);
        } catch (_) {
          return;
        }

        if (payload.type === "segment") {
          segmentsReceived.add(1);

          // published_at is a server wall-clock float (seconds since epoch).
          // Comparing with Date.now()/1000 gives us the downstream latency:
          // translation + Redis pub/sub + WebSocket delivery.
          const publishedAt = payload.published_at;
          if (publishedAt !== null && publishedAt !== undefined) {
            const latencyMs = Date.now() - publishedAt * 1000;
            // Sanity filter: ignore negative or absurdly large values that
            // indicate clock skew or a test artefact.
            if (latencyMs >= 0 && latencyMs < 30_000) {
              e2eLatency.add(latencyMs);
            }
          }
        } else if (payload.type === "session_ended") {
          socket.close(1000);
        }
        // Heartbeats and scrollback messages are silently ignored.
      });

      socket.on("close", (code) => {
        activeListeners.add(-1);
        if (code !== 1000 && code !== 1001 && code !== 4410) {
          console.warn(
            `listener[${sessionIdx}/${lang}] closed unexpectedly: code=${code}`
          );
        }
      });

      socket.on("error", (e) => {
        activeListeners.add(-1);
        console.error(`listener[${sessionIdx}/${lang}] WS error: ${e.error}`);
      });

      // Stay alive for the full ramp + hold window.
      const holdMs = (RAMP_UP_SECONDS + HOLD_SECONDS) * 1000;
      socket.setTimeout(() => socket.close(), holdMs);
    }
  );

  wsConnectErrors.add(!(res && res.status === 101));
  check(res, { "listener WS 101": (r) => r && r.status === 101 });
}

// ---------------------------------------------------------------------------
// Teardown — stop all test sessions
// ---------------------------------------------------------------------------

export function teardown(data) {
  if (!data.sessions) return;
  for (const session of data.sessions) {
    const res = http.post(
      `${data.baseUrl}/api/sessions/${session.id}/stop`,
      null,
      {
        headers: { Authorization: `Bearer ${session.token}` },
        tags: { scenario: "teardown" },
      }
    );
    if (res.status !== 200) {
      console.warn(`stop session ${session.id}: ${res.status}`);
    }
  }
  console.log("All test sessions stopped.");
}
