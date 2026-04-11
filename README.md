# Sunday Voice

A self-hosted, real-time translation web app for in-building ward and stake meetings. An operator captures audio, the system transcribes and translates it, and listeners view (and optionally hear) the translated output on their own devices.

**Target languages:** English, Spanish, Tongan, Tagalog  
**Target latency:** 2–3 seconds end-to-end  
**Scale:** 3–5 concurrent sessions, up to ~100 listeners per session

## How it works

1. An operator logs in, creates a session, and starts capturing audio from a microphone or sound board.
2. Audio is sent to the server, transcribed via the OpenAI Whisper API, and translated via Google Cloud Translation.
3. Listeners join anonymously by scanning a QR code or entering a session code — no account required.
4. Each listener picks their language and reads live translated text (with optional TTS audio playback).

## Requirements

- Ubuntu server (LXC on Proxmox or bare metal)
- Python 3.12+
- Node.js 22+ (for building the frontend)
- PostgreSQL 15+
- Redis 7+
- Caddy or Nginx (TLS termination)
- OpenAI API key (Whisper transcription)
- Google Cloud service account with Translation API v3 and Text-to-Speech enabled

## Installation

### Automated (recommended)

Run the install script as root on the target machine. It handles all steps below automatically:

```bash
git clone https://github.com/kalebhall/sunday-voice.git /opt/sunday-voice
bash /opt/sunday-voice/scripts/install.sh
```

Then follow the printed next-step instructions to fill in API keys, run migrations, and start the service.

### Manual

#### 1. Install system packages

```bash
apt update && apt install -y \
  python3.12 python3.12-venv python3.12-dev python3-pip \
  postgresql postgresql-client \
  redis-server \
  git curl ca-certificates gnupg lsb-release \
  build-essential libpq-dev
```

Install Caddy from the official apt repository:

```bash
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

Install Node.js 22 LTS:

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt install -y nodejs
```

#### 2. Clone the repository

```bash
git clone https://github.com/kalebhall/sunday-voice.git /opt/sunday-voice
```

#### 3. Create a system user

```bash
useradd --system --shell /usr/sbin/nologin \
    --home-dir /opt/sunday-voice --create-home \
    sundayvoice
chown -R sundayvoice:sundayvoice /opt/sunday-voice
```

#### 4. Set up the Python virtual environment

```bash
sudo -u sundayvoice python3.12 -m venv /opt/sunday-voice/.venv
sudo -u sundayvoice /opt/sunday-voice/.venv/bin/pip install --upgrade pip
sudo -u sundayvoice /opt/sunday-voice/.venv/bin/pip install /opt/sunday-voice
```

#### 5. Create runtime directories

```bash
install -d -o sundayvoice -g sundayvoice -m 750 /opt/sunday-voice/var
install -d -o sundayvoice -g sundayvoice -m 750 /opt/sunday-voice/var/tts-cache
```

#### 6. Build the frontend

```bash
cd /opt/sunday-voice/frontend
sudo -u sundayvoice npm ci
sudo -u sundayvoice npm run build
```

#### 7. Configure environment variables

```bash
cp /opt/sunday-voice/.env.example /opt/sunday-voice/.env
chown sundayvoice:sundayvoice /opt/sunday-voice/.env
chmod 600 /opt/sunday-voice/.env
```

Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_URL` | Redis connection string |
| `OPENAI_API_KEY` | OpenAI API key for Whisper transcription |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to Google Cloud service account JSON |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud project ID |
| `SECRET_KEY` | Random secret for session signing (generate with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`) |
| `CONTENT_RETENTION_HOURS` | Hours to keep transcript data (default: 48) |
| `MONTHLY_BUDGET_USD` | Monthly API spend alert threshold |

#### 8. Initialize the database

```bash
# Start PostgreSQL
systemctl enable --now postgresql

# Create the role and database
sudo -u postgres psql -c "CREATE ROLE sundayvoice WITH LOGIN PASSWORD 'yourpassword';"
sudo -u postgres psql -c "CREATE DATABASE sundayvoice OWNER sundayvoice;"

# Run migrations
make migrate
```

#### 9. Create the first admin user

```bash
cd /opt/sunday-voice
.venv/bin/python scripts/seed_admin.py \
    --email admin@example.com \
    --display-name "Admin"
```

You will be prompted for a password.

#### 10. Install the systemd services

```bash
cp deploy/systemd/sunday-voice-api.service           /etc/systemd/system/
cp deploy/systemd/sunday-voice-cleanup.service       /etc/systemd/system/
cp deploy/systemd/sunday-voice-cleanup.timer         /etc/systemd/system/
cp deploy/systemd/sunday-voice-frontend-build.service /etc/systemd/system/

systemctl daemon-reload

# Enable and start the cleanup timer (runs hourly)
systemctl enable --now sunday-voice-cleanup.timer

# Enable and start the API
systemctl enable --now sunday-voice-api
```

The four systemd units and their roles:

| Unit | Type | Purpose |
|---|---|---|
| `sunday-voice-api.service` | long-running | FastAPI/uvicorn app — API, WebSockets, static files |
| `sunday-voice-cleanup.service` | one-shot | Deletes transcripts older than the retention window |
| `sunday-voice-cleanup.timer` | timer | Triggers `sunday-voice-cleanup.service` hourly |
| `sunday-voice-frontend-build.service` | one-shot | Builds the React frontend (invoke after frontend changes) |

#### 11. Configure the reverse proxy

Copy the bundled Caddyfile and reload Caddy:

```bash
cp /opt/sunday-voice/deploy/Caddyfile /etc/caddy/Caddyfile
# Edit the domain name in /etc/caddy/Caddyfile if needed
systemctl enable --now caddy
systemctl reload caddy
```

The Caddyfile serves React static assets directly from `frontend/dist` and proxies `/api/*`, `/ws/*`, `/healthz`, and `/readyz` to the app on port 8000. TLS is provisioned automatically by Caddy via Let's Encrypt.

#### 12. Verify the installation

```bash
# Check the API service is running
systemctl status sunday-voice-api

# Hit the health endpoints
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz

# Tail the logs
journalctl -u sunday-voice-api -f
```

Then open your domain in a browser and log in with the admin credentials you set in step 9.

## Updating

```bash
cd /opt/sunday-voice

# Pull latest code
git pull origin main

# Update Python dependencies
sudo -u sundayvoice .venv/bin/pip install .

# Rebuild the frontend if frontend source changed
systemctl start sunday-voice-frontend-build

# Run any new database migrations
make migrate

# Restart the API
systemctl restart sunday-voice-api
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
| Reverse proxy | Caddy |

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
