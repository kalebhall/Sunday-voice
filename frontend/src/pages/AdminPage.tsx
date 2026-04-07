/**
 * Admin dashboard – tabbed single-page layout.
 *
 * Tabs:
 *   Users     – users & roles CRUD
 *   Usage     – per-provider CostMeter spend
 *   Audit     – audit log viewer
 *   Retention – retention policy status
 *   Budget    – monthly cap + alert threshold
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import {
  createUser,
  deactivateUser,
  fetchAuditLogs,
  fetchBudgetSettings,
  fetchRetentionStatus,
  fetchRoles,
  fetchUsage,
  fetchUsers,
  updateBudgetSettings,
  updateUser,
} from "../api/adminApi";
import type {
  AuditLogListOut,
  AuditLogOut,
  BudgetSettingsOut,
  RetentionStatusOut,
  RoleOut,
  UsageSummaryOut,
  UserListOut,
  UserOut,
} from "../api/adminApi";

// ---------------------------------------------------------------------------
// Tab type
// ---------------------------------------------------------------------------

type Tab = "users" | "usage" | "audit" | "retention" | "budget";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function fmtUSD(val: string | number): string {
  return `$${Number(val).toFixed(4)}`;
}

function percentBar(spent: number, budget: number): number {
  if (budget <= 0) return 0;
  return Math.min(100, (spent / budget) * 100);
}

// ---------------------------------------------------------------------------
// Users tab
// ---------------------------------------------------------------------------

interface UserFormState {
  email: string;
  password: string;
  display_name: string;
  role_id: number | "";
}

const BLANK_FORM: UserFormState = {
  email: "",
  password: "",
  display_name: "",
  role_id: "",
};

function UsersTab() {
  const { user: me } = useAuth();
  const [list, setList] = useState<UserListOut | null>(null);
  const [roles, setRoles] = useState<RoleOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<UserFormState>(BLANK_FORM);
  const [editId, setEditId] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [u, r] = await Promise.all([fetchUsers(), fetchRoles()]);
      setList(u);
      setRoles(r.roles);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function openCreate() {
    setEditId(null);
    setForm(BLANK_FORM);
    setFormError(null);
    setShowForm(true);
  }

  function openEdit(u: UserOut) {
    setEditId(u.id);
    setForm({
      email: u.email,
      password: "",
      display_name: u.display_name,
      role_id: u.role.id,
    });
    setFormError(null);
    setShowForm(true);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (form.role_id === "") return;
    setSaving(true);
    setFormError(null);
    try {
      if (editId === null) {
        await createUser({
          email: form.email,
          password: form.password,
          display_name: form.display_name,
          role_id: form.role_id,
        });
      } else {
        const patch: Parameters<typeof updateUser>[1] = {
          display_name: form.display_name,
          role_id: form.role_id,
        };
        if (form.password) patch.password = form.password;
        await updateUser(editId, patch);
      }
      setShowForm(false);
      await load();
    } catch (e) {
      setFormError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleDeactivate(id: number) {
    if (!confirm("Deactivate this user?")) return;
    try {
      await deactivateUser(id);
      await load();
    } catch (e) {
      alert(String(e));
    }
  }

  async function handleToggleActive(u: UserOut) {
    try {
      await updateUser(u.id, { is_active: !u.is_active });
      await load();
    } catch (e) {
      alert(String(e));
    }
  }

  return (
    <section className="adm-section">
      <div className="adm-section-header">
        <h3 className="adm-section-title">Users</h3>
        <div className="adm-section-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void load()}
            disabled={loading}
          >
            {loading ? "Loading…" : "Refresh"}
          </button>
          <button type="button" className="btn btn-primary btn-sm" onClick={openCreate}>
            + New user
          </button>
        </div>
      </div>

      {error && <p className="error-banner">{error}</p>}

      {showForm && (
        <div className="adm-modal-backdrop">
          <div className="adm-modal">
            <h4 className="adm-modal-title">
              {editId === null ? "Create user" : "Edit user"}
            </h4>
            {formError && <p className="error-banner">{formError}</p>}
            <form onSubmit={(e) => void handleSubmit(e)}>
              {editId === null && (
                <div className="form-group">
                  <label className="form-label" htmlFor="user-email">Email</label>
                  <input
                    id="user-email"
                    type="email"
                    className="form-input"
                    required
                    value={form.email}
                    onChange={(e) => setForm({ ...form, email: e.target.value })}
                  />
                </div>
              )}
              <div className="form-group">
                <label className="form-label" htmlFor="user-name">Display name</label>
                <input
                  id="user-name"
                  type="text"
                  className="form-input"
                  required
                  value={form.display_name}
                  onChange={(e) => setForm({ ...form, display_name: e.target.value })}
                />
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="user-role">Role</label>
                <select
                  id="user-role"
                  className="form-select"
                  required
                  value={form.role_id}
                  onChange={(e) => setForm({ ...form, role_id: Number(e.target.value) })}
                >
                  <option value="">Select role…</option>
                  {roles.map((r) => (
                    <option key={r.id} value={r.id}>{r.name}</option>
                  ))}
                </select>
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="user-password">
                  {editId === null ? "Password" : "New password (leave blank to keep)"}
                </label>
                <input
                  id="user-password"
                  type="password"
                  className="form-input"
                  required={editId === null}
                  minLength={8}
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                />
              </div>
              <div className="form-actions">
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => setShowForm(false)}
                >
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary btn-sm" disabled={saving}>
                  {saving ? "Saving…" : editId === null ? "Create" : "Save"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {list && (
        <div className="table-wrap">
          <table className="session-table">
            <thead>
              <tr>
                <th>Email</th>
                <th>Name</th>
                <th>Role</th>
                <th>Status</th>
                <th>Last login</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {list.users.map((u) => (
                <tr key={u.id} className={u.is_active ? "" : "adm-row-inactive"}>
                  <td>{u.email}</td>
                  <td>{u.display_name}</td>
                  <td>
                    <span className={`adm-role-badge adm-role-${u.role.name}`}>
                      {u.role.name}
                    </span>
                  </td>
                  <td>
                    <span className={u.is_active ? "badge badge-active" : "badge badge-ended"}>
                      {u.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td className="text-muted">{fmt(u.last_login_at)}</td>
                  <td className="td-actions">
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => openEdit(u)}
                    >
                      Edit
                    </button>
                    {u.id !== me?.id && (
                      <button
                        type="button"
                        className={u.is_active ? "btn btn-danger btn-sm" : "btn btn-ghost btn-sm"}
                        onClick={() =>
                          u.is_active
                            ? void handleDeactivate(u.id)
                            : void handleToggleActive(u)
                        }
                      >
                        {u.is_active ? "Deactivate" : "Reactivate"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-muted adm-count">{list.total} user{list.total !== 1 ? "s" : ""}</p>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Usage tab
// ---------------------------------------------------------------------------

function UsageTab() {
  const now = new Date();
  const defaultPeriod = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  const [period, setPeriod] = useState(defaultPeriod);
  const [data, setData] = useState<UsageSummaryOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (p: string) => {
    setLoading(true);
    setError(null);
    try {
      setData(await fetchUsage(p));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(period); }, [load, period]);

  const spentPct = data
    ? percentBar(Number(data.total_cost_usd), data.monthly_budget_usd)
    : 0;

  return (
    <section className="adm-section">
      <div className="adm-section-header">
        <h3 className="adm-section-title">Provider spend</h3>
        <div className="adm-section-actions">
          <input
            type="month"
            className="form-input adm-month-input"
            value={period}
            onChange={(e) => setPeriod(e.target.value)}
          />
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void load(period)}
            disabled={loading}
          >
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
      </div>

      {error && <p className="error-banner">{error}</p>}

      {data && (
        <>
          {data.alert_triggered && (
            <div className="adm-alert-banner">
              Budget alert: spend has reached{" "}
              {Math.round(data.alert_threshold * 100)}% of the monthly cap (
              {fmtUSD(data.monthly_budget_usd)}).
            </div>
          )}

          <div className="adm-budget-row">
            <div className="adm-budget-meta">
              <span className="adm-budget-spent">{fmtUSD(data.total_cost_usd)}</span>
              <span className="text-muted"> / {fmtUSD(data.monthly_budget_usd)} budget</span>
            </div>
            <div className="adm-progress-track">
              <div
                className={`adm-progress-bar ${data.alert_triggered ? "adm-progress-bar--warn" : ""}`}
                style={{ width: `${spentPct}%` }}
              />
            </div>
            <span className="text-muted adm-budget-pct">{spentPct.toFixed(1)}%</span>
          </div>

          {data.rows.length === 0 ? (
            <p className="text-muted empty-state">No usage recorded for {data.period}.</p>
          ) : (
            <div className="table-wrap">
              <table className="session-table">
                <thead>
                  <tr>
                    <th>Provider</th>
                    <th>Operation</th>
                    <th>Units</th>
                    <th>Cost (USD)</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((r, i) => (
                    <tr key={i}>
                      <td>{r.provider}</td>
                      <td>{r.operation}</td>
                      <td>{r.units.toLocaleString()}</td>
                      <td>{fmtUSD(r.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="adm-totals-row">
                    <td colSpan={3}>Total</td>
                    <td>{fmtUSD(data.total_cost_usd)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Audit log tab
// ---------------------------------------------------------------------------

function AuditTab() {
  const [data, setData] = useState<AuditLogListOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [actionFilter, setActionFilter] = useState("");
  const PAGE_SIZE = 50;

  const load = useCallback(async (p: number, action: string) => {
    setLoading(true);
    setError(null);
    try {
      setData(await fetchAuditLogs(p, PAGE_SIZE, action || undefined));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(page, actionFilter); }, [load, page, actionFilter]);

  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 1;

  function handleActionChange(e: React.ChangeEvent<HTMLInputElement>) {
    setPage(1);
    setActionFilter(e.target.value);
  }

  function expandedDetails(log: AuditLogOut): string {
    if (!log.details) return "";
    return JSON.stringify(log.details, null, 2);
  }

  return (
    <section className="adm-section">
      <div className="adm-section-header">
        <h3 className="adm-section-title">Audit log</h3>
        <div className="adm-section-actions">
          <input
            type="text"
            className="form-input adm-filter-input"
            placeholder="Filter by action…"
            value={actionFilter}
            onChange={handleActionChange}
          />
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void load(page, actionFilter)}
            disabled={loading}
          >
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
      </div>

      {error && <p className="error-banner">{error}</p>}

      {data && (
        <>
          {data.logs.length === 0 ? (
            <p className="text-muted empty-state">No audit entries found.</p>
          ) : (
            <div className="table-wrap">
              <table className="session-table adm-audit-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Action</th>
                    <th>Actor</th>
                    <th>Target</th>
                    <th>IP</th>
                    <th>Details</th>
                  </tr>
                </thead>
                <tbody>
                  {data.logs.map((log) => (
                    <tr key={log.id}>
                      <td className="adm-audit-ts text-muted">{fmt(log.created_at)}</td>
                      <td>
                        <code className="adm-action-code">{log.action}</code>
                      </td>
                      <td className="text-muted">
                        {log.actor_email ?? (log.actor_user_id != null ? `#${log.actor_user_id}` : "system")}
                      </td>
                      <td className="text-muted">
                        {log.target_type
                          ? `${log.target_type}${log.target_id ? `:${log.target_id}` : ""}`
                          : "—"}
                      </td>
                      <td className="text-muted">{log.ip_address ?? "—"}</td>
                      <td>
                        {log.details ? (
                          <details className="adm-details-toggle">
                            <summary>View</summary>
                            <pre className="adm-details-pre">{expandedDetails(log)}</pre>
                          </details>
                        ) : (
                          <span className="text-muted">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="adm-pagination">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={page <= 1 || loading}
              onClick={() => setPage((p) => p - 1)}
            >
              Previous
            </button>
            <span className="text-muted">
              Page {page} of {totalPages} ({data.total} entries)
            </span>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={page >= totalPages || loading}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </button>
          </div>
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Retention tab
// ---------------------------------------------------------------------------

function RetentionTab() {
  const [data, setData] = useState<RetentionStatusOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await fetchRetentionStatus());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  return (
    <section className="adm-section">
      <div className="adm-section-header">
        <h3 className="adm-section-title">Retention policy</h3>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => void load()}
          disabled={loading}
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {error && <p className="error-banner">{error}</p>}

      {data && (
        <div className="adm-kv-grid">
          <div className="adm-kv-row">
            <span className="adm-kv-label">Content retention</span>
            <span className="adm-kv-value">{data.retention_hours} hours</span>
          </div>
          <div className="adm-kv-row">
            <span className="adm-kv-label">Cleanup job</span>
            <span className="adm-kv-value">
              {data.cleanup_enabled ? (
                <span className="badge badge-active">Enabled</span>
              ) : (
                <span className="badge badge-ended">Disabled</span>
              )}
              {data.cleanup_enabled &&
                ` — runs every ${data.cleanup_interval_minutes} min`}
            </span>
          </div>
          <div className="adm-kv-row">
            <span className="adm-kv-label">Last cleanup run</span>
            <span className="adm-kv-value">
              {data.last_cleanup ? (
                <>
                  {fmt(data.last_cleanup.created_at)}
                  {data.last_cleanup.details && (
                    <span className="text-muted adm-retention-detail">
                      {" — "}
                      {(data.last_cleanup.details as {
                        transcript_segments_deleted?: number;
                        translation_segments_deleted?: number;
                        sessions_purged?: number;
                      }).transcript_segments_deleted ?? 0} transcript,{" "}
                      {(data.last_cleanup.details as {
                        translation_segments_deleted?: number;
                      }).translation_segments_deleted ?? 0} translation segments deleted,{" "}
                      {(data.last_cleanup.details as {
                        sessions_purged?: number;
                      }).sessions_purged ?? 0} sessions purged
                    </span>
                  )}
                </>
              ) : (
                <span className="text-muted">Never</span>
              )}
            </span>
          </div>
          <div className="adm-kv-row">
            <span className="adm-kv-label">Policy note</span>
            <span className="adm-kv-value text-muted">
              Transcript and translation content is deleted after{" "}
              {data.retention_hours}h. Audit logs and aggregate usage stats are
              retained indefinitely.
            </span>
          </div>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Budget tab
// ---------------------------------------------------------------------------

function BudgetTab() {
  const [data, setData] = useState<BudgetSettingsOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [budget, setBudget] = useState("");
  const [threshold, setThreshold] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await fetchBudgetSettings();
      setData(d);
      setBudget(String(d.monthly_budget_usd));
      setThreshold(String(Math.round(d.alert_threshold * 100)));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    const budgetVal = Number(budget);
    const thresholdVal = Number(threshold) / 100;
    if (budgetVal <= 0 || thresholdVal <= 0 || thresholdVal > 1) {
      setSaveError("Budget must be > 0 and threshold must be 1–100%.");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await updateBudgetSettings({
        monthly_budget_usd: budgetVal,
        alert_threshold: thresholdVal,
      });
      setData(updated);
      setEditing(false);
    } catch (e) {
      setSaveError(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="adm-section">
      <div className="adm-section-header">
        <h3 className="adm-section-title">Budget &amp; alerts</h3>
        <div className="adm-section-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void load()}
            disabled={loading}
          >
            {loading ? "Loading…" : "Refresh"}
          </button>
          {!editing && (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => setEditing(true)}
            >
              Edit
            </button>
          )}
        </div>
      </div>

      {error && <p className="error-banner">{error}</p>}

      {data && !editing && (
        <div className="adm-kv-grid">
          <div className="adm-kv-row">
            <span className="adm-kv-label">Monthly budget cap</span>
            <span className="adm-kv-value">{fmtUSD(data.monthly_budget_usd)}</span>
          </div>
          <div className="adm-kv-row">
            <span className="adm-kv-label">Alert threshold</span>
            <span className="adm-kv-value">
              {Math.round(data.alert_threshold * 100)}%
            </span>
          </div>
          <div className="adm-kv-row">
            <span className="adm-kv-label">Settings source</span>
            <span className="adm-kv-value">
              {data.source === "db" ? (
                <span className="badge badge-active">Runtime (DB override)</span>
              ) : (
                <span className="badge badge-draft">Environment variable default</span>
              )}
            </span>
          </div>
          <div className="adm-kv-row">
            <span className="adm-kv-label">Note</span>
            <span className="adm-kv-value text-muted">
              An alert is triggered when the current-month spend reaches{" "}
              {Math.round(data.alert_threshold * 100)}% of the cap. DB
              overrides take effect immediately and survive restarts.
            </span>
          </div>
        </div>
      )}

      {editing && (
        <form onSubmit={(e) => void handleSave(e)} className="adm-budget-form">
          {saveError && <p className="error-banner">{saveError}</p>}
          <div className="form-group">
            <label className="form-label" htmlFor="budget-cap">
              Monthly budget cap (USD)
            </label>
            <input
              id="budget-cap"
              type="number"
              className="form-input"
              required
              min="0.01"
              step="0.01"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
            />
            <span className="form-hint">Maximum expected spend per calendar month.</span>
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="budget-threshold">
              Alert threshold (%)
            </label>
            <input
              id="budget-threshold"
              type="number"
              className="form-input"
              required
              min="1"
              max="100"
              step="1"
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
            />
            <span className="form-hint">
              Send alert when spend reaches this % of the cap (e.g. 80).
            </span>
          </div>
          <div className="form-actions">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => { setEditing(false); setSaveError(null); }}
            >
              Cancel
            </button>
            <button type="submit" className="btn btn-primary btn-sm" disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Page root
// ---------------------------------------------------------------------------

const TABS: { id: Tab; label: string }[] = [
  { id: "users", label: "Users" },
  { id: "usage", label: "Usage" },
  { id: "audit", label: "Audit log" },
  { id: "retention", label: "Retention" },
  { id: "budget", label: "Budget" },
];

export default function AdminPage() {
  const { user, logout } = useAuth();
  const [tab, setTab] = useState<Tab>("users");

  return (
    <div className="op-layout">
      <header className="op-header">
        <span className="op-brand">Sunday Voice — Admin</span>
        <div className="op-header-actions">
          <Link to="/" className="btn btn-ghost btn-sm">Sessions</Link>
          <span className="op-user-name">{user?.display_name}</span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>

      <main className="op-main">
        <nav className="adm-tabs" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              className={`adm-tab ${tab === t.id ? "adm-tab--active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <div className="adm-content">
          {tab === "users" && <UsersTab />}
          {tab === "usage" && <UsageTab />}
          {tab === "audit" && <AuditTab />}
          {tab === "retention" && <RetentionTab />}
          {tab === "budget" && <BudgetTab />}
        </div>
      </main>
    </div>
  );
}
