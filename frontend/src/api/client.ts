import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

/**
 * In-memory token store. Tokens never touch localStorage/sessionStorage —
 * a page refresh forces re-login, which is acceptable for an operator console
 * that runs in a single long-lived tab.
 */
let accessToken: string | null = null;
let refreshToken: string | null = null;
let expiresAt = 0; // epoch ms

export function setTokens(access: string, refresh: string, expiresIn: number) {
  accessToken = access;
  refreshToken = refresh;
  // Refresh a bit early (30 s buffer) to avoid racing the server.
  expiresAt = Date.now() + (expiresIn - 30) * 1000;
}

export function clearTokens() {
  accessToken = null;
  refreshToken = null;
  expiresAt = 0;
}

export function hasTokens(): boolean {
  return accessToken !== null;
}

export function getAccessToken(): string | null {
  return accessToken;
}

/** Attempt a silent token refresh. Returns true on success. */
async function tryRefresh(): Promise<boolean> {
  if (!refreshToken) return false;
  const res = await fetch("/api/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  if (!res.ok) {
    clearTokens();
    return false;
  }
  const data = await res.json();
  setTokens(data.access_token, data.refresh_token, data.expires_in);
  return true;
}

let refreshPromise: Promise<boolean> | null = null;

const authMiddleware: Middleware = {
  async onRequest({ request }) {
    // Proactively refresh if the access token is about to expire.
    if (accessToken && Date.now() >= expiresAt) {
      // Deduplicate concurrent refresh calls.
      if (!refreshPromise) {
        refreshPromise = tryRefresh().finally(() => {
          refreshPromise = null;
        });
      }
      await refreshPromise;
    }

    if (accessToken) {
      request.headers.set("Authorization", `Bearer ${accessToken}`);
    }
    return request;
  },

  async onResponse({ response }) {
    // If we get a 401, try one refresh then retry the original request.
    if (response.status === 401 && refreshToken) {
      if (!refreshPromise) {
        refreshPromise = tryRefresh().finally(() => {
          refreshPromise = null;
        });
      }
      const ok = await refreshPromise;
      if (!ok) {
        clearTokens();
      }
    }
    return response;
  },
};

const api = createClient<paths>({ baseUrl: "/" });
api.use(authMiddleware);

export default api;
