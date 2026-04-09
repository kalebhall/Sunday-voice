# Sunday Voice

A self-hosted, real-time translation web app for in-building ward and stake meetings. An operator captures audio, the system transcribes and translates it, and listeners view (and optionally hear) the translated output on their own devices.

**Target languages:** English, Spanish, Samoan, Tagalog  
**Target latency:** 2–3 seconds end-to-end  
**Scale:** 3–5 concurrent sessions, up to ~100 listeners per session

## How it works

1. An operator logs in, creates a session, and starts capturing audio from a microphone or sound board.
2. Audio is sent to the server, transcribed via the OpenAI Whisper API, and translated via Google Cloud Translation.
3. Listeners join anonymously by scanning a QR code or entering a session code — no account required.
4. Each listener picks their language and reads live translated text (with optional TTS audio playback).

## Requirements

- Ubuntu server (LXC on Proxmox or bare metal)
- Python 3.11+
- Node.js 20+ (for building the frontend)
- PostgreSQL 15+
- Redis 7+
- Caddy or Nginx (TLS termination)
- OpenAI API key (Whisper transcription)
- Google Cloud service account with Translation API v3 and Text-to-Speech enabled

## Installation

### 1. Install system packages

```bash
apt update && apt install -y \
  python3 python3-venv python3-pip \
  postgresql redis-server \
  caddy git curl
```

INSTALL NODE.JS (LTS)

```
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node -v
npm -v
```

### 2. Clone the repository

```bash
git clone https://github.com/kalebhall/sunday-voice.git /opt/sunday-voice
```

### 3. Create a system user

```bash
useradd --system --home /opt/sunday-voice --shell /bin/bash sunday-voice
chown -R sunday-voice:sunday-voice /opt/sunday-voice
```

### 4. Set up the Python virtual environment

```bash
cd /opt/sunday-voice
make install
```

This creates `.venv` and installs all backend dependencies from `pyproject.toml`.

### 5. Build the frontend

```bash
cd /opt/sunday-voice/frontend
npm ci
npm run build
```

### 6. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_URL` | Redis connection string |
| `OPENAI_API_KEY` | OpenAI API key for Whisper transcription |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to Google Cloud service account JSON |
| `SECRET_KEY` | Random secret for session signing |
| `RETENTION_HOURS` | Hours to keep transcript data (default: 48) |
| `COST_BUDGET_USD` | Monthly API spend alert threshold |

### 7. Initialize the database

```bash
# Create the PostgreSQL role and database
sudo -u postgres psql -c "CREATE USER sunday_voice WITH PASSWORD 'yourpassword';"
sudo -u postgres psql -c "CREATE DATABASE sunday_voice OWNER sunday_voice;"

# Run migrations
make migrate
```

### 8. Install the systemd service

```bash
cp deploy/sunday-voice.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sunday-voice
```

### 9. Configure the reverse proxy

Point Caddy or Nginx at the app's local port (default `8000`). Example Caddyfile:

```
translate.yourdomain.com {
    reverse_proxy localhost:8000
}
```

### 10. Verify the installation

```bash
# Check the service is running
systemctl status sunday-voice

# Hit the health endpoint
curl http://localhost:8000/health

# Tail the logs
journalctl -u sunday-voice -f
```

Then open your domain in a browser and log in with the initial admin credentials printed during `migrate`.

## Updating

```bash
cd /opt/sunday-voice

# Pull latest code
git pull origin main

# Update Python dependencies
make install

# Rebuild the frontend (if changed)
make frontend-build

# Run any new database migrations
make migrate

# Restart the service
systemctl restart sunday-voice
```

## Architecture overview

| Component | Technology |
|---|---|
| API + WebSockets | Python + FastAPI (async) |
| Frontend | React + Vite + TypeScript |
| Database | PostgreSQL |
| Pub/sub + session state | Redis |
| Transcription | OpenAI Whisper API |
| Translation | Google Cloud Translation API v3 |
| TTS (optional) | Google Cloud Text-to-Speech |
| Reverse proxy | Caddy or Nginx |

See [`docs/architecture.md`](docs/architecture.md) for a full description of the data flow and component boundaries.

## Documentation

| Topic | File |
|---|---|
| Product requirements | [`docs/product-requirements.md`](docs/product-requirements.md) |
| System architecture | [`docs/architecture.md`](docs/architecture.md) |
| Provider strategy | [`docs/provider-strategy.md`](docs/provider-strategy.md) |
| Operator workflow | [`docs/operator-workflow.md`](docs/operator-workflow.md) |
| Listener experience | [`docs/listener-experience.md`](docs/listener-experience.md) |
| Deployment details | [`docs/deployment.md`](docs/deployment.md) |
| Security and privacy | [`docs/security-and-privacy.md`](docs/security-and-privacy.md) |
| Roadmap | [`docs/roadmap.md`](docs/roadmap.md) |
