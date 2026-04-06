import { createBrowserRouter, Navigate, Outlet } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import LoginPage from "./pages/LoginPage";
import DashboardPage from "./pages/DashboardPage";
import SessionFormPage from "./pages/SessionFormPage";
import ConsolePage from "./pages/ConsolePage";

/** Redirect to /login when not authenticated. */
function RequireAuth() {
  const { user, loading } = useAuth();
  if (loading) return null; // wait for /me probe
  if (!user) return <Navigate to="/login" replace />;
  return <Outlet />;
}

/** Redirect authenticated users away from the login page. */
function GuestOnly() {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (user) return <Navigate to="/" replace />;
  return <Outlet />;
}

const router = createBrowserRouter([
  {
    element: <GuestOnly />,
    children: [{ path: "/login", element: <LoginPage /> }],
  },
  {
    element: <RequireAuth />,
    children: [
      { path: "/", element: <DashboardPage /> },
      { path: "/sessions/new", element: <SessionFormPage /> },
      { path: "/sessions/:id/edit", element: <SessionFormPage /> },
      { path: "/sessions/:id/console", element: <ConsolePage /> },
    ],
  },
]);

export default router;
