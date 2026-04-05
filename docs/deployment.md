# Sunday Voice – Deployment

## Target Environment

- Host: Personal Ubuntu server.
- Virtualization: LXC container on Proxmox.
- Deployment: Docker Compose inside the container.
- Domain: translate.thestand.app (or similar).

## Services

- `web`: Python/FastAPI app + WebSockets + UI.
- `db`: PostgreSQL.
- `redis`: optional, for pub/sub and caching.
- `proxy`: Nginx or Caddy for TLS.

## Steps (High-Level)

1. Create LXC container with Ubuntu.
2. Install Docker and Docker Compose.
3. Clone `kalebhall/sunday-voice` repo.
4. Copy `.env.example` to `.env` and configure:
   - DB credentials.
   - Redis connection (if used).
   - Whisper API key.
   - Translation provider keys.
   - Retention and budget config.
5. Run `docker compose up -d`.
6. Configure Nginx/Caddy to:
   - Terminate TLS for translate.thestand.app.
   - Proxy `https://translate.thestand.app` to the web container.
7. Confirm:
   - Health endpoints.
   - Operator login.
   - Test session.

## Operations

- Updates:
  - Pull latest code.
  - Rebuild and restart containers.
- Backups:
  - Nightly PostgreSQL backups.
  - Backup configuration files.
- Monitoring:
  - Collect logs from containers.
  - Expose metrics endpoint.
