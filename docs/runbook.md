# Sunday Voice – Operator Runbook

This runbook covers day-to-day operations for a production Sunday Voice instance
running on Ubuntu LXC.  All commands assume you are logged in as **root** (or
using `sudo`) on the host that runs the service.

---

## 1. Start / Stop / Restart

### Application service

```bash
# Start
systemctl start sunday-voice-api

# Stop (drains in-flight requests; ~5 s graceful timeout)
systemctl stop sunday-voice-api

# Restart (e.g. after config or code change)
systemctl restart sunday-voice-api

# Check status
systemctl status sunday-voice-api

# Tail live logs
journalctl -u sunday-voice-api -f
```

### Dependencies

```bash
# PostgreSQL
systemctl start|stop|restart postgresql

# Redis
systemctl start|stop|restart redis-server

# Caddy (reverse proxy / TLS)
systemctl start|stop|reload caddy   # prefer reload over restart to keep TLS state
```

### Full stack restart order

Bring infrastructure up before the application; bring the application down
before infrastructure when stopping.

```bash
# Bring up
systemctl start postgresql redis-server
systemctl start sunday-voice-api

# Bring down
systemctl stop sunday-voice-api
systemctl stop redis-server postgresql
```

### Health check

```bash
curl -sf http://localhost:8000/healthz && echo OK
curl -sf http://localhost:8000/readyz  && echo READY
```

`/healthz` returns 200 when the process is alive.  `/readyz` additionally
verifies the database connection and Redis reachability.

---

## 2. Deploy / Update

Run the deployment script as root.  It is idempotent and safe to re-run.

```bash
cd /opt/sunday-voice
sudo bash scripts/deploy.sh
```

The script:
1. `git pull` (resets to `origin/main`)
2. `pip install` (inside `.venv`)
3. `alembic upgrade head` (migrations)
4. Rebuilds the React frontend if `frontend/` files changed
5. `systemctl restart sunday-voice-api`
6. Reloads Caddy if the `Caddyfile` changed

Check the result:

```bash
journalctl -u sunday-voice-api -n 50
curl -sf http://localhost:8000/readyz && echo READY
```

---

## 3. Rotate API Keys

All secrets are stored in `/opt/sunday-voice/.env`.  The application reads them
at startup, so a restart is required after any change.

### 3a. Rotate `SECRET_KEY` (JWT signing)

Rotating `SECRET_KEY` invalidates **all existing JWTs**.  Operators will be
logged out and must re-authenticate.  Listeners are anonymous and unaffected.

```bash
# Generate a new key
NEW_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
echo "New SECRET_KEY: $NEW_KEY"

# Edit .env
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$NEW_KEY|" /opt/sunday-voice/.env

# Restart to pick up the change
systemctl restart sunday-voice-api
```

Confirm login still works after restart before ending the maintenance window.

### 3b. Rotate `OPENAI_API_KEY` (Whisper transcription)

```bash
# Update the key in .env
sed -i "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=<new-key>|" /opt/sunday-voice/.env

# Hot-reload: the provider is instantiated per-request so a restart picks it up
systemctl restart sunday-voice-api
```

Verify: start a session and stream a short audio clip; confirm transcription
segments appear without provider errors in the logs.

### 3c. Rotate Google Cloud credentials (Translation + TTS)

1. In Google Cloud Console, create a new service-account key (JSON).
2. Copy the file to the server:
   ```bash
   scp new-credentials.json sundayvoice@<host>:/opt/sunday-voice/google-credentials.json
   chmod 600 /opt/sunday-voice/google-credentials.json
   chown sundayvoice:sundayvoice /opt/sunday-voice/google-credentials.json
   ```
3. Update `.env`:
   ```bash
   sed -i "s|^GOOGLE_APPLICATION_CREDENTIALS=.*|GOOGLE_APPLICATION_CREDENTIALS=/opt/sunday-voice/google-credentials.json|" \
       /opt/sunday-voice/.env
   ```
4. Restart the application:
   ```bash
   systemctl restart sunday-voice-api
   ```
5. Delete the old service-account key from Google Cloud Console and remove
   the old credentials file from the server.

### 3d. Verify after any key rotation

```bash
# Service is up
systemctl is-active sunday-voice-api

# No credential errors in logs (last 2 min)
journalctl -u sunday-voice-api --since "2 minutes ago" | grep -iE 'error|credential|auth'
```

---

## 4. Rollback

Use this procedure when a deploy introduces a regression and you need to revert
to the previous working version.

### 4a. Identify the previous good commit

```bash
cd /opt/sunday-voice
git log --oneline -10
```

Note the commit SHA of the last known-good release, e.g. `abc1234`.

### 4b. Roll back code

```bash
cd /opt/sunday-voice
sudo -u sundayvoice git checkout abc1234
sudo -u sundayvoice .venv/bin/pip install --quiet .
```

### 4c. Roll back the database (if migrations were applied)

Check the current Alembic revision:

```bash
sudo -u sundayvoice bash scripts/run-migrations.sh current
```

List available revisions:

```bash
sudo -u sundayvoice bash scripts/run-migrations.sh history --verbose
```

Downgrade to the revision that was active before the bad deploy.  The revision
ID appears in `backend/alembic/versions/`.

```bash
sudo -u sundayvoice bash scripts/run-migrations.sh downgrade <revision-id>
```

> **Warning**: downgrade scripts that drop columns or tables are destructive.
> Restore from the nightly backup first if data loss is unacceptable (see §5).

### 4d. Restart and verify

```bash
systemctl restart sunday-voice-api
sleep 3
systemctl status sunday-voice-api
curl -sf http://localhost:8000/readyz && echo READY
```

### 4e. Re-pin the branch (optional)

If the regression is on `main`, open a fix PR rather than leaving the server on
a detached HEAD.  Once the fix is merged, re-deploy with `scripts/deploy.sh`.

---

## 5. Backup and Restore

### 5a. Manual backup

```bash
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="/var/backups/sunday-voice/db-$TIMESTAMP.sql.gz"

mkdir -p /var/backups/sunday-voice
sudo -u postgres pg_dump sundayvoice | gzip > "$BACKUP_FILE"
echo "Backup written to $BACKUP_FILE"
```

Include the `.env` file in a separate, encrypted backup:

```bash
cp /opt/sunday-voice/.env "/var/backups/sunday-voice/env-$TIMESTAMP"
# Encrypt if storing off-host:
# gpg --symmetric "/var/backups/sunday-voice/env-$TIMESTAMP"
```

### 5b. Automated nightly backups (recommended)

Install the systemd timer (once):

```bash
cat > /etc/systemd/system/sunday-voice-backup.service << 'EOF'
[Unit]
Description=Sunday Voice nightly PostgreSQL backup

[Service]
Type=oneshot
User=postgres
ExecStart=/bin/bash -c 'mkdir -p /var/backups/sunday-voice && \
  pg_dump sundayvoice | gzip > /var/backups/sunday-voice/db-$(date +%%Y%%m%%d).sql.gz && \
  find /var/backups/sunday-voice -name "db-*.sql.gz" -mtime +7 -delete'
EOF

cat > /etc/systemd/system/sunday-voice-backup.timer << 'EOF'
[Unit]
Description=Sunday Voice nightly backup timer

[Timer]
OnCalendar=03:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now sunday-voice-backup.timer
```

### 5c. Restore from backup

**Stop the application first** to prevent writes during restore.

```bash
systemctl stop sunday-voice-api

# Drop and recreate the database
sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='sundayvoice' AND pid <> pg_backend_pid();"
sudo -u postgres psql -c "DROP DATABASE sundayvoice;"
sudo -u postgres psql -c "CREATE DATABASE sundayvoice OWNER sundayvoice;"

# Restore
gunzip -c /var/backups/sunday-voice/db-<TIMESTAMP>.sql.gz | sudo -u postgres psql sundayvoice

# Re-run migrations to bring schema to current HEAD (safe if already at head)
sudo -u sundayvoice bash /opt/sunday-voice/scripts/run-migrations.sh upgrade head

# Restart
systemctl start sunday-voice-api
sleep 3
curl -sf http://localhost:8000/readyz && echo READY
```

---

## 6. Monitoring and Logs

### Prometheus metrics

Available at `http://localhost:8000/metrics`.  Key metrics:

| Metric | Description |
|---|---|
| `active_sessions` | Number of currently active sessions |
| `connected_listeners` | Total open listener WebSocket connections |
| `segment_pipeline_duration_seconds` | End-to-end audio→translated-segment latency |
| `provider_errors_total` | Provider API error count, labelled by provider |

### Log locations

```bash
# Application (structured JSON)
journalctl -u sunday-voice-api -f

# PostgreSQL
journalctl -u postgresql -f

# Redis
journalctl -u redis-server -f

# Caddy (access + TLS logs)
journalctl -u caddy -f
```

### Audit logs

Audit events (logins, session lifecycle, config changes) are stored in the
`audit_logs` table.  Query via the admin API:

```
GET /api/admin/audit-logs?page=1&action=session.start
Authorization: Bearer <admin-jwt>
```

---

## 7. Common Troubleshooting

| Symptom | Check |
|---|---|
| Service won't start | `journalctl -u sunday-voice-api -n 50`; look for missing env vars or DB connection errors |
| 502 from Caddy | Caddy is up but the app isn't: `systemctl status sunday-voice-api` |
| Listeners not receiving segments | Redis pub/sub: `redis-cli monitor`; translation service logs |
| Transcription errors | Check `OPENAI_API_KEY` is set and quota not exceeded: `journalctl -u sunday-voice-api | grep whisper` |
| Translation errors | Check Google credentials file exists and `GOOGLE_CLOUD_PROJECT` is set |
| Database connection refused | `systemctl status postgresql`; verify `DATABASE_URL` in `.env` |
| High memory / OOM | Check audio chunk queue backlog; consider reducing `OPERATOR_AUDIO_MAX_BYTES_PER_MINUTE` |
