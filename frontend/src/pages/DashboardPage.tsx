import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import api from "../api/client";
import type { components } from "../api/schema";

type SessionOut = components["schemas"]["SessionOut"];

const LANG_NAMES: Record<string, string> = {
  en: "English",
  es: "Spanish",
  sm: "Samoan",
  tl: "Tagalog",
};

function langLabel(code: string): string {
  return LANG_NAMES[code] ?? code;
}

function statusBadgeClass(status: string): string {
  if (status === "active") return "badge badge-active";
  if (status === "draft") return "badge badge-draft";
  return "badge badge-ended";
}

function statusText(status: string): string {
  if (status === "active") return "● Active";
  if (status === "draft") return "Draft";
  return "Ended";
}

export default function DashboardPage() {
  const { user, logout } = useAuth();
  const [sessions, setSessions] = useState<SessionOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    const { data, error: apiErr } = await api.GET("/api/sessions");
    if (apiErr || !data) {
      setError("Failed to load sessions.");
    } else {
      setSessions(data.sessions);
    }
    setLoading(false);
  }

  useEffect(() => {
    void load();
  }, []);

  return (
    <div className="op-layout">
      <header className="op-header">
        <span className="op-brand">Sunday Voice</span>
        <div className="op-header-actions">
          <span className="op-user-name">{user?.display_name}</span>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={logout}
          >
            Sign out
          </button>
        </div>
      </header>

      <main className="op-main">
        <div className="page-toolbar">
          <h2 className="page-title">Sessions</h2>
          <div className="page-toolbar-end">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => void load()}
              disabled={loading}
            >
              {loading ? "Loading…" : "Refresh"}
            </button>
            <Link to="/sessions/new" className="btn btn-primary btn-sm">
              + New session
            </Link>
          </div>
        </div>

        {error && <p className="error-banner">{error}</p>}

        {!loading && !error && sessions.length === 0 && (
          <p className="text-muted empty-state">
            No sessions yet.{" "}
            <Link to="/sessions/new">Create one</Link> to get started.
          </p>
        )}

        {sessions.length > 0 && (
          <div className="table-wrap">
            <table className="session-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Status</th>
                  <th>Languages</th>
                  <th>Transport</th>
                  <th>Scheduled</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((s) => (
                  <tr key={s.id}>
                    <td className="td-name">{s.name}</td>
                    <td>
                      <span className={statusBadgeClass(s.status)}>
                        {statusText(s.status)}
                      </span>
                    </td>
                    <td className="td-langs">
                      <span className="lang-src">
                        {langLabel(s.source_language)}
                      </span>
                      {s.target_languages.length > 0 && (
                        <>
                          {" → "}
                          {s.target_languages
                            .map((l) => langLabel(l.language_code))
                            .join(", ")}
                        </>
                      )}
                    </td>
                    <td className="td-transport">
                      {s.audio_transport === "websocket_chunks"
                        ? "WebSocket"
                        : "WebRTC"}
                    </td>
                    <td className="td-scheduled">
                      {s.scheduled_at
                        ? new Date(s.scheduled_at).toLocaleString()
                        : "—"}
                    </td>
                    <td className="td-actions">
                      {s.status === "draft" && (
                        <Link
                          to={`/sessions/${s.id}/edit`}
                          className="btn btn-ghost btn-sm"
                        >
                          Edit
                        </Link>
                      )}
                      {s.status !== "ended" && (
                        <Link
                          to={`/sessions/${s.id}/console`}
                          className="btn btn-primary btn-sm"
                        >
                          Console
                        </Link>
                      )}
                      {s.status === "ended" && (
                        <span className="text-muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </div>
  );
}
