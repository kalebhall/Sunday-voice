import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import api from "../api/client";
import type { components } from "../api/schema";

type LanguageConfig = components["schemas"]["LanguageConfig"];

const SUPPORTED_LANGUAGES = [
  { code: "en", name: "English" },
  { code: "es", name: "Spanish" },
  { code: "sm", name: "Samoan" },
  { code: "tl", name: "Tagalog" },
];

export default function SessionFormPage() {
  const { id } = useParams<{ id?: string }>();
  const navigate = useNavigate();
  const isEdit = Boolean(id);

  const [name, setName] = useState("");
  const [sourceLanguage, setSourceLanguage] = useState("en");
  const [audioTransport, setAudioTransport] = useState("websocket_chunks");
  const [targetLanguages, setTargetLanguages] = useState<LanguageConfig[]>([]);
  const [scheduledAt, setScheduledAt] = useState("");
  const [pageLoading, setPageLoading] = useState(isEdit);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isEdit || !id) return;
    void api
      .GET("/api/sessions/{session_id}", {
        params: { path: { session_id: id } },
      })
      .then(({ data }) => {
        if (data) {
          setName(data.name);
          setSourceLanguage(data.source_language);
          setAudioTransport(data.audio_transport);
          setTargetLanguages(data.target_languages);
          if (data.scheduled_at) {
            setScheduledAt(data.scheduled_at.slice(0, 16));
          }
        }
        setPageLoading(false);
      });
  }, [id, isEdit]);

  function toggleTargetLang(code: string) {
    setTargetLanguages((prev) => {
      const exists = prev.find((l) => l.language_code === code);
      if (exists) return prev.filter((l) => l.language_code !== code);
      return [...prev, { language_code: code, tts_enabled: false }];
    });
  }

  function toggleTts(code: string) {
    setTargetLanguages((prev) =>
      prev.map((l) =>
        l.language_code === code ? { ...l, tts_enabled: !l.tts_enabled } : l,
      ),
    );
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      if (isEdit && id) {
        const { error: err } = await api.PATCH(
          "/api/sessions/{session_id}",
          {
            params: { path: { session_id: id } },
            body: {
              name,
              source_language: sourceLanguage,
              audio_transport: audioTransport,
              target_languages: targetLanguages,
            },
          },
        );
        if (err) {
          throw new Error(
            (err as { detail?: string }).detail ?? "Update failed",
          );
        }
      } else {
        const { error: err } = await api.POST("/api/sessions", {
          body: {
            name,
            source_language: sourceLanguage,
            audio_transport: audioTransport,
            target_languages: targetLanguages,
            scheduled_at: scheduledAt || null,
          },
        });
        if (err) {
          throw new Error(
            (err as { detail?: string }).detail ?? "Create failed",
          );
        }
      }
      navigate("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSubmitting(false);
    }
  }

  if (pageLoading) {
    return <p className="page-loading text-muted">Loading session…</p>;
  }

  const enabledCodes = new Set(targetLanguages.map((l) => l.language_code));
  const availableTargets = SUPPORTED_LANGUAGES.filter(
    (l) => l.code !== sourceLanguage,
  );

  return (
    <div className="op-layout">
      <header className="op-header">
        <Link to="/" className="op-back-link">
          ← Sessions
        </Link>
      </header>

      <main className="op-main">
        <h2 className="page-title">
          {isEdit ? "Edit Session" : "New Session"}
        </h2>

        <form className="session-form" onSubmit={handleSubmit}>
          <div className="form-group">
            <label className="form-label" htmlFor="sess-name">
              Name
            </label>
            <input
              id="sess-name"
              className="form-input"
              type="text"
              required
              maxLength={120}
              placeholder="Sunday 9 AM Sacrament Meeting"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={submitting}
            />
          </div>

          <div className="form-group">
            <label className="form-label" htmlFor="sess-src-lang">
              Source language
            </label>
            <select
              id="sess-src-lang"
              className="form-select"
              value={sourceLanguage}
              onChange={(e) => {
                setSourceLanguage(e.target.value);
                // Drop any target that matches the new source
                setTargetLanguages((prev) =>
                  prev.filter((l) => l.language_code !== e.target.value),
                );
              }}
              disabled={submitting}
            >
              {SUPPORTED_LANGUAGES.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.name}
                </option>
              ))}
            </select>
          </div>

          <div className="form-group">
            <span className="form-label">Target languages</span>
            <div className="lang-grid">
              {availableTargets.map((l) => {
                const enabled = enabledCodes.has(l.code);
                const ttsEntry = targetLanguages.find(
                  (t) => t.language_code === l.code,
                );
                return (
                  <div
                    key={l.code}
                    className={`lang-option${enabled ? " lang-option--on" : ""}`}
                  >
                    <label className="lang-option-check">
                      <input
                        type="checkbox"
                        checked={enabled}
                        onChange={() => toggleTargetLang(l.code)}
                        disabled={submitting}
                      />
                      <span className="lang-option-name">{l.name}</span>
                    </label>
                    {enabled && (
                      <label className="lang-tts-toggle">
                        <input
                          type="checkbox"
                          checked={ttsEntry?.tts_enabled ?? false}
                          onChange={() => toggleTts(l.code)}
                          disabled={submitting}
                        />
                        <span>TTS audio</span>
                      </label>
                    )}
                  </div>
                );
              })}
            </div>
            {availableTargets.length === 0 && (
              <p className="text-muted form-hint">
                No target languages available for this source.
              </p>
            )}
          </div>

          <div className="form-group">
            <span className="form-label">Audio transport</span>
            <div className="radio-group">
              <label className="radio-option">
                <input
                  type="radio"
                  name="transport"
                  value="websocket_chunks"
                  checked={audioTransport === "websocket_chunks"}
                  onChange={() => setAudioTransport("websocket_chunks")}
                  disabled={submitting}
                />
                <span>
                  WebSocket chunks{" "}
                  <span className="text-muted">(default, broad compatibility)</span>
                </span>
              </label>
              <label className="radio-option">
                <input
                  type="radio"
                  name="transport"
                  value="webrtc"
                  checked={audioTransport === "webrtc"}
                  onChange={() => setAudioTransport("webrtc")}
                  disabled={submitting}
                />
                <span>
                  WebRTC{" "}
                  <span className="text-muted">(lower latency, requires STUN)</span>
                </span>
              </label>
              <label className="radio-option">
                <input
                  type="radio"
                  name="transport"
                  value="web_speech"
                  checked={audioTransport === "web_speech"}
                  onChange={() => setAudioTransport("web_speech")}
                  disabled={submitting}
                />
                <span>
                  Browser (Web Speech API){" "}
                  <span className="text-muted">(fallback when Whisper is unavailable; Chrome/Edge only)</span>
                </span>
              </label>
            </div>
          </div>

          {!isEdit && (
            <div className="form-group">
              <label className="form-label" htmlFor="sess-scheduled">
                Scheduled start{" "}
                <span className="text-muted">(optional)</span>
              </label>
              <input
                id="sess-scheduled"
                className="form-input"
                type="datetime-local"
                value={scheduledAt}
                onChange={(e) => setScheduledAt(e.target.value)}
                disabled={submitting}
              />
            </div>
          )}

          {error && <p className="error-banner">{error}</p>}

          <div className="form-actions">
            <Link to="/" className="btn btn-ghost">
              Cancel
            </Link>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={submitting}
            >
              {submitting
                ? "Saving…"
                : isEdit
                  ? "Save changes"
                  : "Create session"}
            </button>
          </div>
        </form>
      </main>
    </div>
  );
}
