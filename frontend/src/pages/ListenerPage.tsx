/**
 * Anonymous listener view — /l/:code
 *
 * Flow:
 *   loading → not_found | waiting (draft) | pick_language (active) | ended
 *   pick_language → listening
 *   listening → ended (on session stop)
 *
 * Features:
 *  - Language picker with all session languages
 *  - Live caption view with scrollback (handles WS "scrollback" + "segment" messages)
 *  - Auto-scroll that pauses when the user scrolls up, resumes on reach-bottom
 *  - TTS audio playback (FIFO queue, skips segments >10 s stale)
 *  - QR code panel for sharing the join link
 *  - Accessible large-text presentation mode
 *  - Thumbs-down quality feedback per segment
 *  - Auto-reconnect with exponential back-off; detects session end
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { QRCodeSVG } from "qrcode.react";

// ─── Types ────────────────────────────────────────────────────────────────────

interface LanguageInfo {
  language_code: string;
  tts_enabled: boolean;
}

interface ListenerSession {
  id: string;
  name: string;
  source_language: string;
  status: "draft" | "active" | "ended";
  target_languages: LanguageInfo[];
  started_at: string | null;
}

interface Segment {
  seq: number;
  text: string;
  segment_id: number | null;
  tts_url: string | null;
  arrivedAt: number;
  thumbedDown: boolean;
}

type Phase =
  | "loading"
  | "not_found"
  | "waiting"
  | "pick_language"
  | "listening"
  | "ended";

// ─── Constants ────────────────────────────────────────────────────────────────

const LANG_NAMES: Record<string, string> = {
  en: "English",
  es: "Spanish",
  sm: "Samoan",
  tl: "Tagalog",
  fr: "French",
  pt: "Portuguese",
  zh: "Chinese",
  ja: "Japanese",
  ko: "Korean",
  ar: "Arabic",
  ru: "Russian",
  de: "German",
  it: "Italian",
};

function langLabel(code: string): string {
  return LANG_NAMES[code] ?? code.toUpperCase();
}

function wsBaseUrl(): string {
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}`;
}

// ─── TTS queue hook ───────────────────────────────────────────────────────────

function useTtsQueue() {
  const queue = useRef<Array<{ url: string; arrivedAt: number }>>([]);
  const playing = useRef(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const drain = useCallback(() => {
    const item = queue.current.shift();
    if (!item) {
      playing.current = false;
      return;
    }
    // Skip segments that arrived more than 10 seconds ago.
    if (Date.now() - item.arrivedAt > 10_000) {
      drain();
      return;
    }
    playing.current = true;
    const audio = new Audio(item.url);
    audioRef.current = audio;
    audio.onended = drain;
    audio.onerror = drain;
    audio.play().catch(drain);
  }, []);

  const enqueue = useCallback(
    (url: string, arrivedAt: number) => {
      queue.current.push({ url, arrivedAt });
      if (!playing.current) drain();
    },
    [drain],
  );

  const stop = useCallback(() => {
    queue.current = [];
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    playing.current = false;
  }, []);

  return { enqueue, stop };
}

// ─── Component ────────────────────────────────────────────────────────────────

export default function ListenerPage() {
  const { code } = useParams<{ code: string }>();

  // ── State ──────────────────────────────────────────────────────────────────
  const [phase, setPhase] = useState<Phase>("loading");
  const [session, setSession] = useState<ListenerSession | null>(null);
  const [selectedLang, setSelectedLang] = useState<string | null>(null);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [wsStatus, setWsStatus] = useState<"connecting" | "open" | "closed">(
    "closed",
  );
  const [ttsOn, setTtsOn] = useState(false);
  const [presentationMode, setPresentationMode] = useState(false);
  const [showQr, setShowQr] = useState(false);

  // ── Refs ───────────────────────────────────────────────────────────────────
  const mountedRef = useRef(true);
  const wsRef = useRef<WebSocket | null>(null);
  const lastSeqRef = useRef(0);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectDelayRef = useRef(2000);
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const sessionRef = useRef<ListenerSession | null>(null);
  const selectedLangRef = useRef<string | null>(null);
  const ttsOnRef = useRef(false);

  const { enqueue: enqueueTts, stop: stopTts } = useTtsQueue();

  // Keep refs in sync
  useEffect(() => {
    sessionRef.current = session;
  }, [session]);
  useEffect(() => {
    selectedLangRef.current = selectedLang;
  }, [selectedLang]);
  useEffect(() => {
    ttsOnRef.current = ttsOn;
  }, [ttsOn]);

  // ── Mount/unmount cleanup ─────────────────────────────────────────────────
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      wsRef.current?.close();
      stopTts();
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Fetch session ──────────────────────────────────────────────────────────
  const fetchSession = useCallback(async () => {
    if (!code || !mountedRef.current) return;
    try {
      const res = await fetch(`/api/sessions/join/${encodeURIComponent(code)}`);
      if (!mountedRef.current) return;
      if (res.status === 404) {
        setPhase("not_found");
        return;
      }
      if (!res.ok) {
        setPhase("not_found");
        return;
      }
      const data = (await res.json()) as ListenerSession;
      setSession(data);
      if (data.status === "ended") {
        setPhase("ended");
      } else if (data.status === "active") {
        setPhase((prev) =>
          prev === "listening" ? "listening" : "pick_language",
        );
      } else {
        setPhase("waiting");
      }
    } catch {
      if (mountedRef.current) setPhase("not_found");
    }
  }, [code]);

  // Initial load
  useEffect(() => {
    void fetchSession();
  }, [fetchSession]);

  // Poll while waiting for session to go active
  useEffect(() => {
    if (phase !== "waiting") return;
    pollTimerRef.current = setTimeout(() => {
      if (mountedRef.current) void fetchSession();
    }, 5000);
    return () => {
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, [phase, fetchSession]);

  // ── WebSocket ──────────────────────────────────────────────────────────────
  const addSegments = useCallback(
    (incoming: Omit<Segment, "thumbedDown">[]) => {
      setSegments((prev) => {
        const seqSet = new Set(prev.map((s) => s.seq));
        const fresh = incoming.filter((s) => !seqSet.has(s.seq));
        if (fresh.length === 0) return prev;
        return [...prev, ...fresh.map((s) => ({ ...s, thumbedDown: false }))].sort(
          (a, b) => a.seq - b.seq,
        );
      });

      // TTS enqueue for live/scrollback segments
      for (const seg of incoming) {
        if (seg.tts_url && ttsOnRef.current) {
          enqueueTts(seg.tts_url, seg.arrivedAt);
        }
      }
    },
    [enqueueTts],
  );

  const connectWs = useCallback(
    (lang: string) => {
      if (!code || !mountedRef.current) return;
      wsRef.current?.close();

      const qs =
        lastSeqRef.current > 0 ? `?after_seq=${lastSeqRef.current}` : "";
      const url = `${wsBaseUrl()}/ws/listen/${encodeURIComponent(code)}/${encodeURIComponent(lang)}${qs}`;
      const ws = new WebSocket(url);
      wsRef.current = ws;
      setWsStatus("connecting");

      ws.onopen = () => {
        if (!mountedRef.current) { ws.close(); return; }
        setWsStatus("open");
        reconnectDelayRef.current = 2000;
      };

      ws.onmessage = (e) => {
        if (!mountedRef.current) return;
        let msg: Record<string, unknown>;
        try {
          msg = JSON.parse(e.data as string) as Record<string, unknown>;
        } catch {
          return;
        }

        const now = Date.now();

        if (msg.type === "scrollback") {
          const segs = (
            msg.segments as Array<Record<string, unknown>>
          ).map((s) => ({
            seq: s.seq as number,
            text: s.text as string,
            segment_id: (s.segment_id as number | null) ?? null,
            tts_url: (s.tts_url as string | null) ?? null,
            arrivedAt: now,
          }));
          addSegments(segs);
          if (segs.length > 0) {
            lastSeqRef.current = Math.max(
              lastSeqRef.current,
              ...segs.map((s) => s.seq),
            );
          }
        } else if (msg.type === "segment") {
          const seq = msg.seq as number;
          if (seq > lastSeqRef.current) lastSeqRef.current = seq;
          addSegments([
            {
              seq,
              text: msg.text as string,
              segment_id: (msg.segment_id as number | null) ?? null,
              tts_url: (msg.tts_url as string | null) ?? null,
              arrivedAt: now,
            },
          ]);
        }
        // heartbeat: ignore
      };

      ws.onclose = () => {
        if (!mountedRef.current) return;
        setWsStatus("closed");
        wsRef.current = null;

        // Re-check session status before deciding to reconnect.
        void (async () => {
          if (!mountedRef.current || !code) return;
          try {
            const res = await fetch(
              `/api/sessions/join/${encodeURIComponent(code)}`,
            );
            if (!mountedRef.current) return;
            if (res.ok) {
              const data = (await res.json()) as ListenerSession;
              setSession(data);
              if (data.status === "ended") {
                setPhase("ended");
                return;
              }
            }
          } catch {
            // Network error — still try to reconnect
          }

          // Session is still active or unreachable — reconnect with back-off.
          const delay = reconnectDelayRef.current;
          reconnectDelayRef.current = Math.min(delay * 2, 30_000);
          reconnectTimerRef.current = setTimeout(() => {
            const l = selectedLangRef.current;
            if (mountedRef.current && l) connectWs(l);
          }, delay);
        })();
      };

      ws.onerror = () => {
        // onclose fires after onerror; handled there.
      };
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [code, addSegments],
  );

  // Connect when language is selected
  useEffect(() => {
    if (phase !== "listening" || !selectedLang) return;
    connectWs(selectedLang);
    return () => {
      wsRef.current?.close();
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, selectedLang]);

  // ── Auto-scroll ────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!atBottomRef.current) return;
    const el = scrollRef.current;
    if (el) {
      setTimeout(() => {
        el.scrollTop = el.scrollHeight;
      }, 20);
    }
  }, [segments]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    const threshold = 60;
    atBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
  }

  // ── Thumbs-down feedback ───────────────────────────────────────────────────
  function handleThumbsDown(seq: number) {
    setSegments((prev) =>
      prev.map((s) => (s.seq === seq ? { ...s, thumbedDown: true } : s)),
    );
    const seg = segments.find((s) => s.seq === seq);
    if (!seg || seg.segment_id == null || !session) return;

    void fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        segment_id: seg.segment_id,
        language_code: selectedLang,
        session_id: session.id,
      }),
    }).catch(() => {
      // Best-effort — don't surface errors to the listener
    });
  }

  // ── Language selection ─────────────────────────────────────────────────────
  function selectLanguage(lang: string) {
    setSelectedLang(lang);
    setSegments([]);
    lastSeqRef.current = 0;
    reconnectDelayRef.current = 2000;
    setPhase("listening");
  }

  // ── All available languages for the session ────────────────────────────────
  function allLanguages(): LanguageInfo[] {
    if (!session) return [];
    const src: LanguageInfo = {
      language_code: session.source_language,
      tts_enabled: false,
    };
    return [src, ...session.target_languages];
  }

  function ttsEnabledForLang(lang: string): boolean {
    if (!session) return false;
    return session.target_languages.some(
      (l) => l.language_code === lang && l.tts_enabled,
    );
  }

  // ── TTS toggle ─────────────────────────────────────────────────────────────
  function toggleTts() {
    if (ttsOn) {
      stopTts();
      setTtsOn(false);
    } else {
      setTtsOn(true);
    }
  }

  // ── Presentation mode ──────────────────────────────────────────────────────
  const visibleSegments = presentationMode
    ? segments.slice(-3)
    : segments;

  // ── Render ─────────────────────────────────────────────────────────────────

  if (phase === "loading") {
    return (
      <div className="lp-shell">
        <p className="lp-status-msg">Loading…</p>
      </div>
    );
  }

  if (phase === "not_found") {
    return (
      <div className="lp-shell">
        <p className="lp-status-msg lp-status-msg--error">
          Session not found. Check the code and try again.
        </p>
      </div>
    );
  }

  if (phase === "waiting" && session) {
    return (
      <div className="lp-shell">
        <header className="lp-header">
          <span className="lp-brand">Sunday Voice</span>
          <span className="lp-session-name">{session.name}</span>
        </header>
        <main className="lp-waiting">
          <div className="lp-waiting-icon" aria-hidden="true">🕐</div>
          <h1 className="lp-waiting-title">Session not started yet</h1>
          <p className="lp-waiting-sub">
            The operator hasn't started the session. This page will update
            automatically when it begins.
          </p>
        </main>
      </div>
    );
  }

  if (phase === "pick_language" && session) {
    const langs = allLanguages();
    return (
      <div className="lp-shell">
        <header className="lp-header">
          <span className="lp-brand">Sunday Voice</span>
          <span className="lp-session-name">{session.name}</span>
        </header>
        <main className="lp-pick">
          <h1 className="lp-pick-title">Choose your language</h1>
          <div className="lp-lang-grid" role="list">
            {langs.map((l) => (
              <button
                key={l.language_code}
                type="button"
                className="lp-lang-btn"
                role="listitem"
                onClick={() => selectLanguage(l.language_code)}
              >
                <span className="lp-lang-name">{langLabel(l.language_code)}</span>
                {l.tts_enabled && (
                  <span className="lp-lang-tts-badge" title="Audio available">
                    🔊
                  </span>
                )}
              </button>
            ))}
          </div>

          <div className="lp-qr-section">
            <p className="lp-qr-label">Share this session</p>
            <QRCodeSVG value={window.location.href} size={160} />
            <p className="lp-qr-url">{window.location.href}</p>
          </div>
        </main>
      </div>
    );
  }

  if (phase === "ended") {
    return (
      <div className="lp-shell">
        <header className="lp-header">
          <span className="lp-brand">Sunday Voice</span>
          {session && <span className="lp-session-name">{session.name}</span>}
        </header>
        <main className="lp-waiting">
          <div className="lp-waiting-icon" aria-hidden="true">✓</div>
          <h1 className="lp-waiting-title">Session ended</h1>
          <p className="lp-waiting-sub">Thanks for joining.</p>
        </main>
      </div>
    );
  }

  // ── Listening view ─────────────────────────────────────────────────────────
  if (phase !== "listening" || !session || !selectedLang) return null;

  const hasTts = ttsEnabledForLang(selectedLang);

  return (
    <div
      className={`lp-shell lp-shell--listening ${presentationMode ? "lp-presentation" : ""}`}
    >
      {/* ── Header ── */}
      {!presentationMode && (
        <header className="lp-header lp-header--listening">
          <div className="lp-header-left">
            <span className="lp-brand">Sunday Voice</span>
            <span className="lp-session-name">{session.name}</span>
          </div>
          <div className="lp-header-right">
            <span
              className={`lp-ws-dot lp-ws-dot--${wsStatus}`}
              title={`Connection: ${wsStatus}`}
            />
            {hasTts && (
              <button
                type="button"
                className={`lp-icon-btn ${ttsOn ? "lp-icon-btn--on" : ""}`}
                onClick={toggleTts}
                aria-pressed={ttsOn}
                title={ttsOn ? "Mute audio" : "Enable audio"}
              >
                {ttsOn ? "🔊" : "🔇"}
              </button>
            )}
            <button
              type="button"
              className="lp-icon-btn"
              onClick={() => setShowQr((v) => !v)}
              title="Show QR code"
              aria-pressed={showQr}
            >
              QR
            </button>
            <button
              type="button"
              className="lp-icon-btn"
              onClick={() => setPresentationMode(true)}
              title="Presentation mode"
            >
              ⛶
            </button>
            <button
              type="button"
              className="lp-lang-chip"
              onClick={() => {
                wsRef.current?.close();
                wsRef.current = null;
                setPhase("pick_language");
                setSelectedLang(null);
                setSegments([]);
              }}
              title="Change language"
            >
              {langLabel(selectedLang)}
            </button>
          </div>
        </header>
      )}

      {/* ── Presentation mode header ── */}
      {presentationMode && (
        <header className="lp-pres-header">
          <button
            type="button"
            className="lp-pres-exit"
            onClick={() => setPresentationMode(false)}
          >
            Exit presentation
          </button>
          <span className="lp-pres-lang">{langLabel(selectedLang)}</span>
        </header>
      )}

      {/* ── QR panel ── */}
      {showQr && !presentationMode && (
        <div className="lp-qr-panel" role="dialog" aria-label="QR code">
          <QRCodeSVG value={window.location.href} size={200} />
          <p className="lp-qr-url">{window.location.href}</p>
          <button
            type="button"
            className="lp-qr-close"
            onClick={() => setShowQr(false)}
          >
            Close
          </button>
        </div>
      )}

      {/* ── Caption scroll area ── */}
      <main
        ref={scrollRef}
        className="lp-captions"
        onScroll={handleScroll}
        aria-live="polite"
        aria-label={`${langLabel(selectedLang)} captions`}
      >
        {visibleSegments.length === 0 ? (
          <p className="lp-caption-empty">
            {wsStatus === "open"
              ? "Waiting for speech…"
              : wsStatus === "connecting"
                ? "Connecting…"
                : "Reconnecting…"}
          </p>
        ) : (
          visibleSegments.map((seg) => (
            <div key={seg.seq} className="lp-caption-row">
              <p className="lp-caption-text">{seg.text}</p>
              {!presentationMode && seg.segment_id != null && (
                <button
                  type="button"
                  className={`lp-thumb-btn ${seg.thumbedDown ? "lp-thumb-btn--down" : ""}`}
                  onClick={() => handleThumbsDown(seg.seq)}
                  title="Flag poor translation"
                  aria-label="Flag poor translation"
                  disabled={seg.thumbedDown}
                >
                  👎
                </button>
              )}
            </div>
          ))
        )}
      </main>
    </div>
  );
}
