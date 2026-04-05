# Sunday Voice – Deployment

## Target Environment

- Host: Personal Ubuntu server.
- Virtualization: LXC container on Proxmox.
- Deployment: native systemd services (no containers).
- Domain: translate.thestand.app (or similar).

## Services (systemd units)

- `sunday-voice.service`: Python/FastAPI app (uvicorn or gunicorn + uvicorn
  workers) serving the API, WebSockets, and the built React UI as static files.
- `postgresql.service`: PostgreSQL installed from apt.
- `redis-server.service`: Redis installed from apt (used for WebSocket pub/sub
  and ephemeral session state).
- Reverse proxy: Caddy or Nginx installed from apt, providing TLS termination
  and proxying to the app.

## Steps (High-Level)

1. Create LXC container with Ubuntu.
2. Install system packages via apt: `python3`, `python3-venv`, `postgresql`,
   `redis-server`, `caddy` (or `nginx`), `git`, and Node.js (for building the
   frontend).
3. Clone `kalebhall/sunday-voice` into `/opt/sunday-voice`.
4. Create a Python virtual environment and install backend dependencies.
5. Build the frontend (`npm ci && npm run build`) into the static assets
   directory served by FastAPI.
6. Create a dedicated `sunday-voice` system user and give it ownership of the
   app directory and any runtime paths.
7. Copy `.env.example` to `.env` and configure:
   - DB credentials (local Postgres role + database).
   - Redis connection.
   - Whisper API key.
   - Google Cloud service-account credentials (Translation + TTS).
   - Retention and budget thresholds.
8. Initialize the database (create role/db, run migrations).
9. Install the `sunday-voice.service` systemd unit and `systemctl enable --now`
   it.
10. Configure Caddy/Nginx to terminate TLS for translate.thestand.app and
    proxy to the app's local port.
11. Confirm:
    - Health endpoint.
    - Operator login.
    - Test session end-to-end.

## Operations

- Updates:
  - `git pull` in `/opt/sunday-voice`.
  - `pip install -r requirements.txt` inside the venv.
  - `npm ci && npm run build` for frontend changes.
  - Run DB migrations.
  - `systemctl restart sunday-voice`.
- Backups:
  - Nightly PostgreSQL dumps (e.g., `pg_dump` via cron or systemd timer).
  - Backup `.env` and systemd unit files.
- Monitoring:
  - App logs via `journalctl -u sunday-voice`.
  - Expose Prometheus-style metrics endpoint (`/metrics`).
