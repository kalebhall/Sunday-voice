# Sunday Voice – Deployment

## Target Environment

- Host: Ubuntu 24.04 LTS.
- Virtualization: Proxmox LXC.
- Deployment: native systemd services (no containers).
- Checkout: `/opt/sunday-voice`, tracking `origin/main`.
- Domain: `translate.thestand.app`.

## Services (systemd units)

- `sunday-voice-api.service`: Python/FastAPI app (uvicorn) serving the API,
  WebSockets, and the built React UI as static files.
- `postgresql.service`: PostgreSQL installed from apt.
- `redis-server.service`: Redis installed from apt (used for WebSocket pub/sub
  and ephemeral session state).
- `caddy.service`: reverse proxy and TLS termination.

## Steps (High-Level)

1. Create LXC container with Ubuntu.
2. Install system packages via apt: `python3`, `python3-venv`, `postgresql`,
   `redis-server`, `caddy` (or `nginx`), `git`, and Node.js (for building the
   frontend).
3. Clone `kalebhall/sunday-voice` into `/opt/sunday-voice`.
4. Create a Python virtual environment and install backend dependencies.
5. Build the frontend (`npm ci --include=dev && npm run build`) into the static
   assets directory served by FastAPI.
6. Create and use the dedicated `sundayvoice` system user; give it ownership of
   the application checkout, virtual environment, and writable runtime paths.
7. Copy `.env.example` to `.env` and configure:
   - DB credentials (local Postgres role + database).
   - Redis connection.
   - Whisper API key.
   - Google Cloud service-account credentials (Translation + TTS).
   - Retention and budget thresholds.
8. Initialize the database (create role/db, run migrations).
9. Install and enable the `sunday-voice-api.service` systemd unit.
10. Configure Caddy/Nginx to terminate TLS for translate.thestand.app and
    proxy to the app's local port.
11. Confirm:
    - Health endpoint.
    - Operator login.
    - Test session end-to-end.

## Automated deployment

Automatic deployment is **not installed or approved yet**. Production currently has a
manual deployment path only. Any automatic polling design must first harden the Git
trust boundary, migration safety, rollback behavior, and readiness checks.

Use the existing manual deploy entrypoint only with root access:

```bash
cd /opt/sunday-voice
sudo bash scripts/deploy.sh
```

After deployment, verify `sunday-voice-api`, `/healthz`, PostgreSQL, and Redis.
Note: `/readyz` currently returns a placeholder response; it does not verify DB or
Redis connectivity. Do not grant broad sudo access to the `svops` account for
deployment.

## Operations

- Updates:
  - Production currently has no automatic deployment timer installed.
  - Manual: `cd /opt/sunday-voice && sudo bash scripts/deploy.sh`.
  - Verify with `systemctl status sunday-voice-api`,
    `curl -sf http://localhost:8000/healthz`, `pg_isready`, and `redis-cli ping`.
- Backups:
  - Nightly PostgreSQL dumps (e.g., `pg_dump` via cron or systemd timer).
  - Back up `.env` and systemd unit files securely.
- Monitoring:
  - App logs: `journalctl -u sunday-voice-api`.
  - Metrics endpoint: `/metrics`.
