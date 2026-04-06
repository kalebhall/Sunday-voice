import { useAuth } from "../auth/AuthContext";

export default function DashboardPage() {
  const { user, logout } = useAuth();

  return (
    <main className="dashboard">
      <header className="dashboard-header">
        <h1>Sunday Voice</h1>
        <div className="dashboard-user">
          <span>
            {user?.display_name} ({user?.role})
          </span>
          <button type="button" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>
      <section>
        <h2>Sessions</h2>
        <p>Session management will be wired here.</p>
      </section>
    </main>
  );
}
