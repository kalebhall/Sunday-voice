import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";
import api, {
  clearAccessToken,
  setAccessToken,
  tryRefresh,
} from "../api/client";
import type { components } from "../api/schema";

type User = components["schemas"]["MeResponse"];

interface AuthState {
  /** Current authenticated user, or null if not logged in. */
  user: User | null;
  /** True while the initial refresh/hydrate probe is in-flight. */
  loading: boolean;
  /** Login with email + password. Throws on failure. */
  login: (email: string, password: string) => Promise<void>;
  /** Clear tokens and reset state. */
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchMe = useCallback(async () => {
    const { data } = await api.GET("/api/auth/me");
    if (data) {
      setUser(data);
    } else {
      clearAccessToken();
      setUser(null);
    }
  }, []);

  // On mount, ask the server to exchange the refresh cookie for a new
  // access token. If the cookie is missing/expired the call 401s and we
  // fall through to the login page — no token state survives in JS, so
  // a page refresh looks like any other cold start from the client side.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const ok = await tryRefresh();
      if (cancelled) return;
      if (ok) {
        await fetchMe();
      }
      if (!cancelled) setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchMe]);

  const login = useCallback(
    async (email: string, password: string) => {
      const { data, error } = await api.POST("/api/auth/login", {
        body: { email, password },
      });
      if (error || !data) {
        // Surface the backend detail message when available.
        const detail =
          (error as { detail?: string } | undefined)?.detail ??
          "Login failed";
        throw new Error(detail);
      }
      setAccessToken(data.access_token, data.expires_in);
      await fetchMe();
    },
    [fetchMe],
  );

  const logout = useCallback(async () => {
    // Best-effort: clear server-side cookie, then drop in-memory state.
    try {
      await api.POST("/api/auth/logout");
    } catch {
      // Network error — we still want to forget the user locally.
    }
    clearAccessToken();
    setUser(null);
  }, []);

  const value = useMemo<AuthState>(
    () => ({ user, loading, login, logout }),
    [user, loading, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within <AuthProvider>");
  }
  return ctx;
}
