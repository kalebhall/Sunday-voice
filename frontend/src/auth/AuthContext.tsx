import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";
import api, { clearTokens, hasTokens, setTokens } from "../api/client";
import type { components } from "../api/schema";

type User = components["schemas"]["MeResponse"];

interface AuthState {
  /** Current authenticated user, or null if not logged in. */
  user: User | null;
  /** True while the initial /me probe is in-flight. */
  loading: boolean;
  /** Login with email + password. Throws on failure. */
  login: (email: string, password: string) => Promise<void>;
  /** Clear tokens and reset state. */
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  /** Fetch /me using the current access token. */
  const fetchMe = useCallback(async () => {
    const { data } = await api.GET("/api/auth/me");
    if (data) {
      setUser(data);
    } else {
      clearTokens();
      setUser(null);
    }
  }, []);

  // On mount: if we somehow have tokens (shouldn't normally — memory-only),
  // try to hydrate user. Otherwise mark loading done immediately.
  useEffect(() => {
    if (hasTokens()) {
      fetchMe().finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
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
      setTokens(data.access_token, data.refresh_token, data.expires_in);
      await fetchMe();
    },
    [fetchMe],
  );

  const logout = useCallback(() => {
    clearTokens();
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
