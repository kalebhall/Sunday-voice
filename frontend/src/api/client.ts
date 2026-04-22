import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

/**
 * Access token lives only in memory; the refresh token is held in an
 * HttpOnly cookie set by the backend (`sv_refresh`), which JavaScript
 * cannot read. On page load, the app silently calls /api/auth/refresh
 * to exchange the cookie for a fresh access token before showing the UI.
 */
let accessToken: string | null = null;
let expiresAt = 0; // epoch ms

export function setAccessToken(token: string, expiresIn: number): void {
  accessToken = token;
  // Refresh a bit early (30 s buffer) to avoid racing the server.
  expiresAt = Date.now() + (expiresIn - 30) * 1000;
}

export function clearAccessToken(): void {
  accessToken = null;
  expiresAt = 0;
}

export function getAccessToken(): string | null {
  return accessToken;
}

/**
 * Exchange the refresh cookie for a new access token. Returns true on
 * success. The refresh cookie is sent automatically because every request
 * goes out with `credentials: "include"`; the backend rotates the cookie
 * on success and clears it on failure.
 */
export async function tryRefresh(): Promise<boolean> {
  const res = await fetch("/api/auth/refresh", {
    method: "POST",
    credentials: "include",
  });
  if (!res.ok) {
    clearAccessToken();
    return false;
  }
  const data = (await res.json()) as { access_token: string; expires_in: number };
  setAccessToken(data.access_token, data.expires_in);
  return true;
}

let refreshPromise: Promise<boolean> | null = null;

function refreshOnce(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = tryRefresh().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

const authMiddleware: Middleware = {
  async onRequest({ request }) {
    // Proactively refresh if the access token is about to expire.
    if (accessToken && Date.now() >= expiresAt) {
      await refreshOnce();
    }

    if (accessToken) {
      request.headers.set("Authorization", `Bearer ${accessToken}`);
    }
    return request;
  },

  async onResponse({ response }) {
    // If we get a 401, try one refresh. The openapi-fetch middleware can't
    // retry the request itself, but the next call will pick up the new token.
    if (response.status === 401) {
      const ok = await refreshOnce();
      if (!ok) {
        clearAccessToken();
      }
    }
    return response;
  },
};

// `credentials: "include"` ensures the refresh cookie accompanies every
// request to the API (cookie Path is /api/auth so it only goes to the
// auth endpoints that need it).
const api = createClient<paths>({ baseUrl: "/", credentials: "include" });
api.use(authMiddleware);

export default api;
