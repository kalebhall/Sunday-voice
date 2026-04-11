import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import api, { getAccessToken } from "../api/client";
import type { components } from "../api/schema";

// Web Speech API is not yet in lib.dom.d.ts for all envs; declare minimally.
declare global {
  interface Window {
    SpeechRecognition: typeof SpeechRecognition | undefined;
    webkitSpeechRecognition: typeof SpeechRecognition | undefined;
  }
}

type SessionOut = components["schemas"]["SessionOut"];

interface Segment {
  seq: number;
  text: string;
}

type CaptureState = "idle" | "starting" | "active" | "pausing" | "stopping" | "error";

// BCP-47 language tag mapping for Web Speech API
const SPEECH_LANG_TAG: Record<string, string> = {
  en: "en-US",
  es: "es-US",
  sm: "sm-WS",
  tl: "fil-PH",
};
type WsState = "connecting" | "open" | "closed" | "error";

const LANG_NAMES: Record<string, string> = {
  en: "English",
  es: "Spanish",
  sm: "Samoan",
  tl: "Tagalog",
};

function langLabel(code: string): string {
  return LANG_NAMES[code] ?? code;
}

function wsBaseUrl(): string {
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}`;
}

// Opens a listener WebSocket for one language and calls back on segment/state.
function openListenerWs(
  lang: string,
  joinCode: string,
  lastSeq: number,
  onOpen: () => void,
  onSegment: (seg: Segment) => void,
  onClose: () => void,
  onError: () => void,
): WebSocket {
  const qs = lastSeq >= 0 ? `?after_seq=${lastSeq}` : "";
  const ws = new WebSocket(
    `${wsBaseUrl()}/ws/listen/${joinCode}/${lang}${qs}`,
  );
  ws.onopen = onOpen;
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data as string) as {
        type: string;
        seq?: number;
        text?: string;
      };
      if (msg.type === "segment" && msg.seq != null && msg.text) {
        onSegment({ seq: msg.seq, text: msg.text });
      }
    } catch {
      // Ignore parse errors (heartbeats etc.)
    }
  };
  ws.onclose = onClose;
  ws.onerror = onError;
  return ws;
}

export default function ConsolePage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  // ----- Session -----
  const [session, setSession] = useState<SessionOut | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // ----- Capture -----
  const [captureState, setCaptureState] = useState<CaptureState>("idle");
  const [captureError, setCaptureError] = useState<string | null>(null);

  // ----- Mic -----
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState("");

  // ----- Transcript display -----
  const [segments, setSegments] = useState<Map<string, Segment[]>>(new Map());
  const [langWsState, setLangWsState] = useState<Map<string, WsState>>(
    new Map(),
  );

  // ----- Viewer link copy state -----
  const [linkCopied, setLinkCopied] = useState(false);

  // ----- Refs -----
  const mountedRef = useRef(true);
  const captureActiveRef = useRef(false);
  const levelFillRef = useRef<HTMLDivElement>(null);
  const animFrameRef = useRef<number>(0);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const operatorWsRef = useRef<WebSocket | null>(null);
  const peerConnRef = useRef<RTCPeerConnection | null>(null);
  const speechRecogRef = useRef<SpeechRecognition | null>(null);
  const listenerWsMap = useRef<Map<string, WebSocket>>(new Map());
  const lastSeqMap = useRef<Map<string, number>>(new Map());
  const reconnectDelayMap = useRef<Map<string, number>>(new Map());
  const panelRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const sessionRef = useRef<SessionOut | null>(null);

  useEffect(() => {
    sessionRef.current = session;
  }, [session]);

  // Mount/unmount guard
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      stopLevelLoop();
      void teardownAudio();
      for (const ws of listenerWsMap.current.values()) ws.close();
      operatorWsRef.current?.close();
      peerConnRef.current?.close();
      if (recorderRef.current?.state !== "inactive") recorderRef.current?.stop();
      speechRecogRef.current?.stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ----- Load session -----
  useEffect(() => {
    if (!id) return;
    void api
      .GET("/api/sessions/{session_id}", {
        params: { path: { session_id: id } },
      })
      .then(({ data, error }) => {
        if (!mountedRef.current) return;
        if (error || !data) {
          setLoadError("Session not found.");
        } else if (data.status === "ended") {
          setLoadError("This session has already ended.");
        } else {
          setSession(data);
        }
      });
  }, [id]);

  // ----- Enumerate mic devices -----
  useEffect(() => {
    void navigator.mediaDevices
      .enumerateDevices()
      .then((devs) => {
        const inputs = devs.filter((d) => d.kind === "audioinput");
        if (!mountedRef.current) return;
        setDevices(inputs);
        if (inputs.length > 0) setDeviceId(inputs[0].deviceId);
      })
      .catch(() => {
        // Permissions not yet granted; will re-enumerate after getUserMedia
      });
  }, []);

  // ----- Level meter -----
  function startLevelLoop() {
    const analyser = analyserRef.current;
    const fill = levelFillRef.current;
    if (!analyser || !fill) return;
    const buf = new Uint8Array(analyser.fftSize);
    function tick() {
      analyser!.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += (buf[i] - 128) ** 2;
      const rms = Math.sqrt(sum / buf.length) / 128;
      // Scale up: quiet speech is ~0.01–0.05 RMS; fill meter at 0.25+
      fill!.style.width = `${Math.min(rms * 5, 1) * 100}%`;
      animFrameRef.current = requestAnimationFrame(tick);
    }
    animFrameRef.current = requestAnimationFrame(tick);
  }

  function stopLevelLoop() {
    if (animFrameRef.current) {
      cancelAnimationFrame(animFrameRef.current);
      animFrameRef.current = 0;
    }
    if (levelFillRef.current) levelFillRef.current.style.width = "0%";
  }

  function setupAudioMeter(stream: MediaStream) {
    const ctx = new AudioContext();
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    audioCtxRef.current = ctx;
    analyserRef.current = analyser;
    startLevelLoop();
  }

  async function teardownAudio() {
    stopLevelLoop();
    analyserRef.current = null;
    if (audioCtxRef.current) {
      await audioCtxRef.current.close().catch(() => {});
      audioCtxRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
  }

  // ----- WebSocket chunks capture -----
  function startWsChunksCapture(stream: MediaStream, sessionId: string) {
    const token = getAccessToken();
    if (!token) throw new Error("Not authenticated");

    const wsUrl = `${wsBaseUrl()}/ws/operator/${sessionId}/audio?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";
    operatorWsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) { ws.close(); return; }
      const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm";
      const recorder = new MediaRecorder(stream, { mimeType });
      recorderRef.current = recorder;

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0 && ws.readyState === WebSocket.OPEN) {
          ws.send(e.data);
        }
      };
      recorder.onerror = () => {
        if (!mountedRef.current) return;
        setCaptureError("MediaRecorder error");
        setCaptureState("error");
        captureActiveRef.current = false;
      };
      recorder.start(2000); // 2-second chunks
    };

    ws.onerror = () => {
      if (!mountedRef.current) return;
      setCaptureError("Audio WebSocket connection error");
      setCaptureState("error");
      captureActiveRef.current = false;
    };

    ws.onclose = (ev) => {
      if (!mountedRef.current) return;
      if (captureActiveRef.current && !ev.wasClean) {
        setCaptureError("Audio connection lost — click Retry to reconnect");
        setCaptureState("error");
        captureActiveRef.current = false;
      }
    };
  }

  // ----- WebRTC capture -----
  async function startWebRTCCapture(stream: MediaStream, sessionId: string) {
    const pc = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });
    peerConnRef.current = pc;

    stream.getAudioTracks().forEach((track) => pc.addTrack(track, stream));

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    // Wait for ICE gathering to finish (max 10 s)
    await Promise.race([
      new Promise<void>((resolve) => {
        if (pc.iceGatheringState === "complete") { resolve(); return; }
        pc.addEventListener("icegatheringstatechange", function h() {
          if (pc.iceGatheringState === "complete") {
            pc.removeEventListener("icegatheringstatechange", h);
            resolve();
          }
        });
      }),
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("ICE gathering timed out")), 10_000),
      ),
    ]);

    const { data, error } = await api.POST(
      "/api/sessions/{session_id}/webrtc/offer",
      {
        params: { path: { session_id: sessionId } },
        body: {
          sdp: pc.localDescription!.sdp,
          type: pc.localDescription!.type,
        },
      },
    );
    if (error || !data) throw new Error("WebRTC offer exchange failed");

    await pc.setRemoteDescription({
      sdp: data.sdp,
      type: data.type as RTCSdpType,
    });

    pc.oniceconnectionstatechange = () => {
      if (!mountedRef.current) return;
      if (
        captureActiveRef.current &&
        (pc.iceConnectionState === "failed" ||
          pc.iceConnectionState === "disconnected")
      ) {
        setCaptureError("WebRTC connection lost — click Retry to reconnect");
        setCaptureState("error");
        captureActiveRef.current = false;
      }
    };
  }

  // ----- Web Speech API capture -----
  function startWebSpeechCapture(sessionId: string, sourceLanguage: string) {
    const SpeechRecognitionCtor =
      window.SpeechRecognition ?? window.webkitSpeechRecognition;
    if (!SpeechRecognitionCtor) {
      throw new Error(
        "Browser does not support the Web Speech API. Use Chrome or Edge.",
      );
    }

    const token = getAccessToken();
    if (!token) throw new Error("Not authenticated");

    const wsUrl = `${wsBaseUrl()}/ws/operator/${sessionId}/transcript?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(wsUrl);
    operatorWsRef.current = ws;

    const recognition = new SpeechRecognitionCtor();
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.lang = SPEECH_LANG_TAG[sourceLanguage] ?? sourceLanguage;
    speechRecogRef.current = recognition;

    ws.onopen = () => {
      if (!mountedRef.current) { ws.close(); return; }
      recognition.start();
    };

    recognition.onresult = (event: SpeechRecognitionEvent) => {
      if (ws.readyState !== WebSocket.OPEN) return;
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (!result.isFinal) continue;
        const text = result[0].transcript.trim();
        if (text) {
          ws.send(JSON.stringify({ text, language: sourceLanguage }));
        }
      }
    };

    recognition.onerror = (event: SpeechRecognitionErrorEvent) => {
      // "no-speech" and "audio-capture" are transient; log but don't abort.
      if (event.error === "no-speech" || event.error === "audio-capture") {
        return;
      }
      if (!mountedRef.current) return;
      setCaptureError(`Speech recognition error: ${event.error}`);
      setCaptureState("error");
      captureActiveRef.current = false;
    };

    recognition.onend = () => {
      // Auto-restart recognition while the capture is still active.
      if (captureActiveRef.current && mountedRef.current) {
        try {
          recognition.start();
        } catch {
          // Ignore if already started.
        }
      }
    };

    ws.onerror = () => {
      if (!mountedRef.current) return;
      setCaptureError("Transcript WebSocket connection error");
      setCaptureState("error");
      captureActiveRef.current = false;
    };

    ws.onclose = (ev) => {
      recognition.stop();
      if (!mountedRef.current) return;
      if (captureActiveRef.current && !ev.wasClean) {
        setCaptureError("Transcript connection lost — click Retry to reconnect");
        setCaptureState("error");
        captureActiveRef.current = false;
      }
    };
  }

  // ----- Start capture -----
  async function startCapture() {
    if (!session || !id) return;
    setCaptureError(null);
    setCaptureState("starting");

    try {
      // Transition draft → active if needed
      let activeSession = session;
      if (session.status === "draft") {
        const { data, error } = await api.POST(
          "/api/sessions/{session_id}/start",
          { params: { path: { session_id: id } } },
        );
        if (error || !data) throw new Error("Failed to start session");
        activeSession = data;
        setSession(data);
      }

      if (activeSession.audio_transport === "web_speech") {
        // Web Speech API manages the mic internally; skip getUserMedia.
        startWebSpeechCapture(id, activeSession.source_language);
      } else {
        // Get mic stream for audio-based transports.
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: deviceId ? { deviceId: { exact: deviceId } } : true,
          video: false,
        });
        streamRef.current = stream;

        // Re-enumerate after permission grant (labels become available)
        const devs = await navigator.mediaDevices.enumerateDevices();
        const inputs = devs.filter((d) => d.kind === "audioinput");
        if (mountedRef.current) setDevices(inputs);

        setupAudioMeter(stream);

        if (activeSession.audio_transport === "webrtc") {
          await startWebRTCCapture(stream, id);
        } else {
          startWsChunksCapture(stream, id);
        }
      }

      captureActiveRef.current = true;
      setCaptureState("active");
    } catch (err) {
      captureActiveRef.current = false;
      // Clean up partial state
      if (recorderRef.current?.state !== "inactive") recorderRef.current?.stop();
      recorderRef.current = null;
      operatorWsRef.current?.close();
      operatorWsRef.current = null;
      peerConnRef.current?.close();
      peerConnRef.current = null;
      speechRecogRef.current?.stop();
      speechRecogRef.current = null;
      await teardownAudio();
      if (mountedRef.current) {
        setCaptureError(
          err instanceof Error ? err.message : "Failed to start capture",
        );
        setCaptureState("error");
      }
    }
  }

  // ----- Pause capture (stops audio only, session stays active) -----
  async function pauseCapture() {
    setCaptureState("pausing");
    captureActiveRef.current = false;

    if (recorderRef.current?.state !== "inactive") recorderRef.current?.stop();
    recorderRef.current = null;

    operatorWsRef.current?.close();
    operatorWsRef.current = null;

    peerConnRef.current?.close();
    peerConnRef.current = null;

    speechRecogRef.current?.stop();
    speechRecogRef.current = null;

    await teardownAudio();

    setCaptureState("idle");
  }

  // ----- End session (stops audio + ends session + navigates away) -----
  async function endSession() {
    setCaptureState("stopping");
    captureActiveRef.current = false;

    if (recorderRef.current?.state !== "inactive") recorderRef.current?.stop();
    recorderRef.current = null;

    operatorWsRef.current?.close();
    operatorWsRef.current = null;

    peerConnRef.current?.close();
    peerConnRef.current = null;

    speechRecogRef.current?.stop();
    speechRecogRef.current = null;

    await teardownAudio();

    if (id) {
      await api
        .POST("/api/sessions/{session_id}/stop", {
          params: { path: { session_id: id } },
        })
        .catch(() => {}); // Best-effort
    }

    navigate("/");
  }

  // ----- Segment state update -----
  const addSegment = useCallback((lang: string, seg: Segment) => {
    setSegments((prev) => {
      const existing = prev.get(lang) ?? [];
      if (existing.some((s) => s.seq === seg.seq)) return prev;
      const updated = [...existing, seg].sort((a, b) => a.seq - b.seq);
      const next = new Map(prev);
      next.set(lang, updated);
      return next;
    });
    // Auto-scroll the transcript panel
    const panel = panelRefs.current.get(lang);
    if (panel) {
      // Use a small timeout so React has painted the new segment first
      setTimeout(() => {
        panel.scrollTop = panel.scrollHeight;
      }, 30);
    }
  }, []);

  // ----- Listener WebSocket connect logic -----
  const connectListenerWsRef = useRef<(lang: string, joinCode: string) => void>(
    () => {},
  );
  connectListenerWsRef.current = (lang: string, joinCode: string) => {
    if (!mountedRef.current) return;
    setLangWsState((prev) => new Map(prev).set(lang, "connecting"));

    const lastSeq = lastSeqMap.current.get(lang) ?? -1;
    const ws = openListenerWs(
      lang,
      joinCode,
      lastSeq,
      () => {
        if (!mountedRef.current) return;
        reconnectDelayMap.current.set(lang, 2000);
        setLangWsState((prev) => new Map(prev).set(lang, "open"));
      },
      (seg) => {
        if (!mountedRef.current) return;
        lastSeqMap.current.set(
          lang,
          Math.max(lastSeqMap.current.get(lang) ?? -1, seg.seq),
        );
        addSegment(lang, seg);
      },
      () => {
        // onclose: auto-reconnect with exponential backoff
        if (!mountedRef.current) return;
        listenerWsMap.current.delete(lang);
        setLangWsState((prev) => new Map(prev).set(lang, "closed"));
        const sess = sessionRef.current;
        if (sess && sess.status !== "ended") {
          const delay = reconnectDelayMap.current.get(lang) ?? 2000;
          reconnectDelayMap.current.set(lang, Math.min(delay * 2, 30_000));
          setTimeout(() => {
            if (!mountedRef.current) return;
            connectListenerWsRef.current(lang, joinCode);
          }, delay);
        }
      },
      () => {
        if (!mountedRef.current) return;
        setLangWsState((prev) => new Map(prev).set(lang, "error"));
      },
    );
    listenerWsMap.current.set(lang, ws);
  };

  // Connect listener WebSockets when session loads
  useEffect(() => {
    if (!session?.join_code) return;
    const { join_code, source_language, target_languages } = session;
    const langs = [
      source_language,
      ...target_languages.map((l) => l.language_code),
    ];
    for (const lang of langs) {
      if (!listenerWsMap.current.has(lang)) {
        connectListenerWsRef.current(lang, join_code);
      }
    }
    return () => {
      for (const ws of listenerWsMap.current.values()) ws.close();
      listenerWsMap.current.clear();
    };
    // Only re-run if the session id changes (languages are stable post-load)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id]);

  // ----- Shared header -----
  function renderHeader() {
    return (
      <header className="op-header">
        <Link to="/" className="op-back-link">
          ← Sessions
        </Link>
        {session && (
          <div className="op-header-actions">
            <span className="op-session-name">{session.name}</span>
            <span className={`badge badge-${session.status}`}>
              {session.status === "active" ? "● Live" : "Draft"}
            </span>
            {session.join_code && (
              <span className="join-code-badge" title="Listener join code">
                Code:{" "}
                <strong>{session.join_code}</strong>
              </span>
            )}
          </div>
        )}
      </header>
    );
  }

  if (loadError) {
    return (
      <div className="op-layout">
        {renderHeader()}
        <main className="op-main">
          <p className="error-banner">{loadError}</p>
          <Link to="/" className="btn btn-ghost" style={{ marginTop: "1rem" }}>
            Back to sessions
          </Link>
        </main>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="op-layout">
        {renderHeader()}
        <main className="op-main">
          <p className="text-muted">Loading session…</p>
        </main>
      </div>
    );
  }

  const allLangs = [
    session.source_language,
    ...session.target_languages.map((l) => l.language_code),
  ];

  return (
    <div className="op-layout console-layout">
      {renderHeader()}

      <main className="console-main">
        {/* ── Capture controls ── */}
        <section className="console-controls">
          <div className="controls-row">
            {/* Mic picker */}
            <div className="control-block">
              <label className="ctrl-label" htmlFor="mic-select">
                Microphone
              </label>
              <select
                id="mic-select"
                className="form-select mic-select"
                value={deviceId}
                onChange={(e) => setDeviceId(e.target.value)}
                disabled={
                  captureState === "active" || captureState === "starting"
                }
              >
                {devices.length === 0 && (
                  <option value="">Default microphone</option>
                )}
                {devices.map((d) => (
                  <option key={d.deviceId} value={d.deviceId}>
                    {d.label || `Microphone ${d.deviceId.slice(0, 8)}`}
                  </option>
                ))}
              </select>
            </div>

            {/* Level meter */}
            <div className="control-block control-block--level">
              <span className="ctrl-label">Input level</span>
              <div className="level-meter">
                <div className="level-meter-fill" ref={levelFillRef} />
              </div>
            </div>

            {/* Start / Pause / End session */}
            <div className="control-block control-block--action">
              {(captureState === "idle" || captureState === "error") && (
                <div className="capture-btn-group">
                  <button
                    type="button"
                    className="btn btn-primary console-capture-btn"
                    onClick={() => void startCapture()}
                  >
                    {session.status === "draft"
                      ? "Start session"
                      : "Start capture"}
                  </button>
                  {session.status === "active" && (
                    <button
                      type="button"
                      className="btn btn-danger console-capture-btn"
                      onClick={() => void endSession()}
                    >
                      End session
                    </button>
                  )}
                </div>
              )}
              {captureState === "starting" && (
                <button
                  type="button"
                  className="btn btn-primary console-capture-btn"
                  disabled
                >
                  Starting…
                </button>
              )}
              {captureState === "active" && (
                <div className="capture-btn-group">
                  <button
                    type="button"
                    className="btn btn-ghost console-capture-btn"
                    onClick={() => void pauseCapture()}
                  >
                    Pause
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger console-capture-btn"
                    onClick={() => void endSession()}
                  >
                    End session
                  </button>
                </div>
              )}
              {(captureState === "pausing" || captureState === "stopping") && (
                <button
                  type="button"
                  className="btn btn-danger console-capture-btn"
                  disabled
                >
                  {captureState === "pausing" ? "Pausing…" : "Stopping…"}
                </button>
              )}
            </div>
          </div>

          {captureError && (
            <div className="error-banner error-banner--inline">
              {captureError}
              {captureState === "error" && (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => void startCapture()}
                >
                  Retry
                </button>
              )}
            </div>
          )}

          <p className="console-meta text-muted">
            Transport:{" "}
            {session.audio_transport === "webrtc"
              ? "WebRTC"
              : session.audio_transport === "web_speech"
                ? "Browser (Web Speech API)"
                : "WebSocket chunks"}{" "}
            · Source: {langLabel(session.source_language)}
            {session.target_languages.length > 0 &&
              ` · Translations: ${session.target_languages.map((l) => langLabel(l.language_code)).join(", ")}`}
          </p>

          {/* Viewer / listener link */}
          {session.join_slug && (
            <div className="viewer-link-row">
              <span className="ctrl-label">Viewer link</span>
              <a
                href={`/l/${session.join_slug}`}
                target="_blank"
                rel="noreferrer"
                className="viewer-link-url"
                title={`${window.location.origin}/l/${session.join_slug}`}
              >
                {`${window.location.origin}/l/${session.join_slug}`}
              </a>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => {
                  const url = `${window.location.origin}/l/${session.join_slug}`;
                  void navigator.clipboard.writeText(url).then(() => {
                    setLinkCopied(true);
                    setTimeout(() => setLinkCopied(false), 2000);
                  });
                }}
              >
                {linkCopied ? "Copied!" : "Copy link"}
              </button>
              {session.join_code && (
                <span className="join-code-inline text-muted">
                  or code <strong>{session.join_code}</strong>
                </span>
              )}
            </div>
          )}
        </section>

        {/* ── Transcript / translation panels ── */}
        <section className="console-panels">
          {allLangs.map((lang) => {
            const segs = segments.get(lang) ?? [];
            const wsState = langWsState.get(lang);
            const isSource = lang === session.source_language;

            return (
              <div key={lang} className="transcript-panel">
                <div className="transcript-panel-header">
                  <span className="transcript-lang">
                    {langLabel(lang)}
                    {isSource && (
                      <span className="transcript-source-tag"> (source)</span>
                    )}
                  </span>
                  <span
                    className={`ws-dot ws-dot--${wsState ?? "closed"}`}
                    title={`WebSocket: ${wsState ?? "disconnected"}`}
                  />
                </div>
                <div
                  className="transcript-body"
                  ref={(el) => {
                    if (el) panelRefs.current.set(lang, el);
                  }}
                >
                  {segs.length === 0 ? (
                    <p className="transcript-empty text-muted">
                      {captureState === "active"
                        ? "Waiting for speech…"
                        : "No transcript yet"}
                    </p>
                  ) : (
                    segs.map((s) => (
                      <p key={s.seq} className="transcript-segment">
                        {s.text}
                      </p>
                    ))
                  )}
                </div>
              </div>
            );
          })}
        </section>
      </main>
    </div>
  );
}
