/**
 * Fullscreen caption projection view — /l/:code/projection
 *
 * Designed for display on a TV or projector in high-contrast mode.
 * Shows the last N segments in large, white text on black.
 * Controls (language, font size, line count) appear on mouse/touch activity
 * and auto-hide after 3 seconds of inactivity.
 *
 * Uses the same anonymous listener WebSocket as ListenerPage.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

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
}

interface Segment {
  seq: number;
  text: string;
}

type Phase =
  | "loading"
  | "not_found"
  | "waiting"
  | "pick_language"
  | "projecting"
  | "ended";

type FontSize = "sm" | "md" | "lg" | "xl";

// ─── Constants ────────────────────────────────────────────────────────────────

const LANG_NAMES: Record<string, string> = {
  en: "English",
  es: "Spanish",
  to: "Tongan",
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

const FONT_SIZE_CSS: Record<FontSize, string> = {
  sm: "clamp(1.5rem, 3vw, 2rem)",
  md: "clamp(2rem, 4.5vw, 3rem)",
  lg: "clamp(2.75rem, 6vw, 4.5rem)",
  xl: "clamp(3.5rem, 8vw, 6rem)",
};

const CONTROLS_HIDE_MS = 3000;

// ─── Component ────────────────────────────────────────────────────────────────

export default function ProjectionPage() {
  const { code } = useParams<{ code: string }>();

  // ── State ──────────────────────────────────────────────────────────────────
  const [phase, setPhase] = useState<Phase>("loading");
  const [session, setSession] = useState<ListenerSession | null>(null);
  const [selectedLang, setSelectedLang] = useState<string | null>(null);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [wsStatus, setWsStatus] = useState<"connecting" | "open" | "closed">(
    "closed",
  );
  const [fontSize, setFontSize] = useState<FontSize>("lg");
  const [lineCount, setLineCount] = useState<number>(3);
  const [controlsVisible, setControlsVisible] = useState(true);

  // ── Refs ───────────────────────────────────────────────────────────────────
  const mountedRef = useRef(true);
  const wsRef = useRef<WebSocket | null>(null);
  const lastSeqRef = useRef(0);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectDelayRef = useRef(2000);
  const selectedLangRef = useRef<string | null>(null);
  const controlsTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    selectedLangRef.current = selectedLang;
  }, [selectedLang]);

  // ── Mount cleanup ─────────────────────────────────────────────────────────
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      wsRef.current?.close();
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (controlsTimerRef.current) clearTimeout(controlsTimerRef.current);
    };
  }, []);

  // ── Controls auto-hide ─────────────────────────────────────────────────────
  const showControls = useCallback(() => {
    setControlsVisible(true);
    if (controlsTimerRef.current) clearTimeout(controlsTimerRef.current);
    controlsTimerRef.current = setTimeout(() => {
      setControlsVisible(false);
    }, CONTROLS_HIDE_MS);
  }, []);

  useEffect(() => {
    if (phase === "projecting") showControls();
    return () => {
      if (controlsTimerRef.current) clearTimeout(controlsTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  // ── Fetch session ──────────────────────────────────────────────────────────
  const fetchSession = useCallback(async () => {
    if (!code || !mountedRef.current) return;
    try {
      const res = await fetch(`/api/sessions/join/${encodeURIComponent(code)}`);
      if (!mountedRef.current) return;
      if (!res.ok) {
        setPhase("not_found");
        return;
      }
      const data = (await res.json()) as ListenerSession;
      setSession(data);
      if (data.status === "ended") {
        setPhase("ended");
      } else if (data.status === "active") {
        setPhase((prev) => (prev === "projecting" ? "projecting" : "pick_language"));
      } else {
        setPhase("waiting");
      }
    } catch {
      if (mountedRef.current) setPhase("not_found");
    }
  }, [code]);

  useEffect(() => {
    void fetchSession();
  }, [fetchSession]);

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
  const addSegments = useCallback((incoming: Segment[]) => {
    setSegments((prev) => {
      const seqSet = new Set(prev.map((s) => s.seq));
      const fresh = incoming.filter((s) => !seqSet.has(s.seq));
      if (fresh.length === 0) return prev;
      return [...prev, ...fresh].sort((a, b) => a.seq - b.seq);
    });
  }, []);

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
        if (!mountedRef.current) {
          ws.close();
          return;
        }
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
        if (msg.type === "scrollback") {
          const segs = (
            msg.segments as Array<Record<string, unknown>>
          ).map((s) => ({
            seq: s.seq as number,
            text: s.text as string,
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
          addSegments([{ seq, text: msg.text as string }]);
        }
        // heartbeat: ignore
      };

      ws.onclose = () => {
        if (!mountedRef.current) return;
        setWsStatus("closed");
        wsRef.current = null;

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

  useEffect(() => {
    if (phase !== "projecting" || !selectedLang) return;
    connectWs(selectedLang);
    return () => {
      wsRef.current?.close();
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, selectedLang]);

  // ── Language selection ─────────────────────────────────────────────────────
  function selectLanguage(lang: string) {
    setSelectedLang(lang);
    setSegments([]);
    lastSeqRef.current = 0;
    reconnectDelayRef.current = 2000;
    setPhase("projecting");
  }

  function allLanguages(): LanguageInfo[] {
    if (!session) return [];
    const src: LanguageInfo = {
      language_code: session.source_language,
      tts_enabled: false,
    };
    return [src, ...session.target_languages];
  }

  // ── Visible segments ───────────────────────────────────────────────────────
  const visibleSegments = segments.slice(-lineCount);

  // ── Render ─────────────────────────────────────────────────────────────────

  if (phase === "loading") {
    return (
      <div className="proj-shell">
        <p className="proj-status">Loading…</p>
      </div>
    );
  }

  if (phase === "not_found") {
    return (
      <div className="proj-shell">
        <p className="proj-status proj-status--error">Session not found.</p>
      </div>
    );
  }

  if (phase === "waiting" && session) {
    return (
      <div className="proj-shell">
        <p className="proj-status">{session.name} — Waiting for session to start…</p>
      </div>
    );
  }

  if (phase === "ended") {
    return (
      <div className="proj-shell">
        <p className="proj-status">Session ended.</p>
      </div>
    );
  }

  if (phase === "pick_language" && session) {
    return (
      <div className="proj-shell proj-pick">
        <h1 className="proj-pick-title">{session.name}</h1>
        <p className="proj-pick-sub">Choose a language for projection</p>
        <div className="proj-lang-grid">
          {allLanguages().map((l) => (
            <button
              key={l.language_code}
              type="button"
              className="proj-lang-btn"
              onClick={() => selectLanguage(l.language_code)}
            >
              {langLabel(l.language_code)}
            </button>
          ))}
        </div>
      </div>
    );
  }

  // ── Projecting view ────────────────────────────────────────────────────────
  if (phase !== "projecting" || !selectedLang) return null;

  return (
    <div
      className="proj-shell proj-live"
      onMouseMove={showControls}
      onTouchStart={showControls}
    >
      {/* Controls overlay — visible on activity, fades out after idle */}
      <div
        className={`proj-controls ${controlsVisible ? "proj-controls--visible" : ""}`}
      >
        {/* Language */}
        <div className="proj-ctrl-group">
          <label className="proj-ctrl-label" htmlFor="proj-lang">
            Language
          </label>
          <select
            id="proj-lang"
            className="proj-ctrl-select"
            value={selectedLang}
            onChange={(e) => selectLanguage(e.target.value)}
          >
            {allLanguages().map((l) => (
              <option key={l.language_code} value={l.language_code}>
                {langLabel(l.language_code)}
              </option>
            ))}
          </select>
        </div>

        {/* Font size */}
        <div className="proj-ctrl-group">
          <span className="proj-ctrl-label">Size</span>
          <div className="proj-ctrl-btns">
            {(["sm", "md", "lg", "xl"] as FontSize[]).map((sz) => (
              <button
                key={sz}
                type="button"
                className={`proj-ctrl-btn ${fontSize === sz ? "proj-ctrl-btn--active" : ""}`}
                onClick={() => setFontSize(sz)}
              >
                {sz.toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        {/* Line count */}
        <div className="proj-ctrl-group">
          <span className="proj-ctrl-label">Lines</span>
          <div className="proj-ctrl-btns">
            {[1, 2, 3, 4, 5].map((n) => (
              <button
                key={n}
                type="button"
                className={`proj-ctrl-btn ${lineCount === n ? "proj-ctrl-btn--active" : ""}`}
                onClick={() => setLineCount(n)}
              >
                {n}
              </button>
            ))}
          </div>
        </div>

        {/* Connection status dot */}
        <span
          className={`proj-ws-dot proj-ws-dot--${wsStatus}`}
          title={`Connection: ${wsStatus}`}
        />
      </div>

      {/* Caption area */}
      <main
        className="proj-captions"
        aria-live="polite"
        aria-label={`${langLabel(selectedLang)} captions`}
      >
        {visibleSegments.length === 0 ? (
          <p className="proj-caption-empty">
            {wsStatus === "open"
              ? "Waiting for speech…"
              : wsStatus === "connecting"
                ? "Connecting…"
                : "Reconnecting…"}
          </p>
        ) : (
          visibleSegments.map((seg) => (
            <p
              key={seg.seq}
              className="proj-caption-text"
              style={{ fontSize: FONT_SIZE_CSS[fontSize] }}
            >
              {seg.text}
            </p>
          ))
        )}
      </main>
    </div>
  );
}
