/**
 * Typed fetch helpers for admin endpoints.
 *
 * The admin routes aren't in the generated schema.d.ts, so we use bare fetch
 * with the in-memory access token from the API client module.
 */
import { getAccessToken } from "./client";

const BASE = "/api/admin";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface RoleOut {
  id: number;
  name: string;
  description: string | null;
}

export interface UserOut {
  id: number;
  email: string;
  display_name: string;
  role: RoleOut;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface UserListOut {
  users: UserOut[];
  total: number;
}

export interface UserCreate {
  email: string;
  password: string;
  display_name: string;
  role_id: number;
}

export interface UserUpdate {
  display_name?: string;
  role_id?: number;
  is_active?: boolean;
  password?: string;
}

export interface RoleListOut {
  roles: RoleOut[];
}

export interface UsageRowOut {
  provider: string;
  operation: string;
  period: string;
  units: number;
  cost_usd: string; // Decimal serialised as string
}

export interface UsageSummaryOut {
  period: string;
  rows: UsageRowOut[];
  total_cost_usd: string;
  monthly_budget_usd: number;
  alert_threshold: number;
  alert_triggered: boolean;
}

export interface AuditLogOut {
  id: number;
  actor_user_id: number | null;
  actor_email: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  ip_address: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
}

export interface AuditLogListOut {
  logs: AuditLogOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface RetentionStatusOut {
  retention_hours: number;
  cleanup_enabled: boolean;
  cleanup_interval_minutes: number;
  last_cleanup: AuditLogOut | null;
}

export interface BudgetSettingsOut {
  monthly_budget_usd: number;
  alert_threshold: number;
  source: string;
}

export interface BudgetSettingsUpdate {
  monthly_budget_usd: number;
  alert_threshold: number;
}

// ---------------------------------------------------------------------------
// Fetch helper
// ---------------------------------------------------------------------------

async function adminFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getAccessToken();
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers as Record<string, string> | undefined),
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = (body as { detail?: string }).detail ?? res.statusText;
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as unknown as T;
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------

export function fetchUsers(page = 1, pageSize = 50): Promise<UserListOut> {
  return adminFetch(`/users?page=${page}&page_size=${pageSize}`);
}

export function fetchUser(id: number): Promise<UserOut> {
  return adminFetch(`/users/${id}`);
}

export function createUser(payload: UserCreate): Promise<UserOut> {
  return adminFetch("/users", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateUser(id: number, payload: UserUpdate): Promise<UserOut> {
  return adminFetch(`/users/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deactivateUser(id: number): Promise<void> {
  return adminFetch(`/users/${id}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Roles
// ---------------------------------------------------------------------------

export function fetchRoles(): Promise<RoleListOut> {
  return adminFetch("/roles");
}

// ---------------------------------------------------------------------------
// Usage
// ---------------------------------------------------------------------------

export function fetchUsage(period?: string): Promise<UsageSummaryOut> {
  const qs = period ? `?period=${period}` : "";
  return adminFetch(`/usage${qs}`);
}

// ---------------------------------------------------------------------------
// Audit logs
// ---------------------------------------------------------------------------

export function fetchAuditLogs(
  page = 1,
  pageSize = 50,
  action?: string,
  actorUserId?: number,
): Promise<AuditLogListOut> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (action) params.set("action", action);
  if (actorUserId !== undefined) params.set("actor_user_id", String(actorUserId));
  return adminFetch(`/audit-logs?${params.toString()}`);
}

// ---------------------------------------------------------------------------
// Retention
// ---------------------------------------------------------------------------

export function fetchRetentionStatus(): Promise<RetentionStatusOut> {
  return adminFetch("/retention");
}

// ---------------------------------------------------------------------------
// Budget
// ---------------------------------------------------------------------------

export function fetchBudgetSettings(): Promise<BudgetSettingsOut> {
  return adminFetch("/budget");
}

export function updateBudgetSettings(
  payload: BudgetSettingsUpdate,
): Promise<BudgetSettingsOut> {
  return adminFetch("/budget", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}
