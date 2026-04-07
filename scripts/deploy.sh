#!/usr/bin/env bash
# scripts/deploy.sh – Pull latest, migrate, build frontend, restart services.
#
# Run as root (or via sudo) on the target machine:
#   sudo bash scripts/deploy.sh
#
# Typical flow:
#   git pull → pip install → (optional) npm build → alembic migrate → restart
#
# The script is idempotent: running it multiple times is safe.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/sunday-voice}"
APP_USER="${APP_USER:-sundayvoice}"
APP_BRANCH="${APP_BRANCH:-main}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info() { printf '\e[1;32m[deploy]\e[0m %s\n' "$*"; }
die()  { printf '\e[1;31m[deploy]\e[0m FATAL: %s\n' "$*" >&2; exit 1; }

require_root() {
    [[ "$(id -u)" -eq 0 ]] || die "This script must be run as root."
}

as_app() {
    # Run a command as the application user.
    sudo -u "$APP_USER" -- "$@"
}

# ---------------------------------------------------------------------------
# 1. Pull latest code
# ---------------------------------------------------------------------------
pull_code() {
    info "Pulling latest code on branch '$APP_BRANCH'..."
    as_app git -C "$APP_DIR" fetch origin
    as_app git -C "$APP_DIR" checkout "$APP_BRANCH"
    as_app git -C "$APP_DIR" reset --hard "origin/$APP_BRANCH"
    info "Code updated to $(as_app git -C "$APP_DIR" rev-parse --short HEAD)."
}

# ---------------------------------------------------------------------------
# 2. Install / upgrade Python dependencies
# ---------------------------------------------------------------------------
install_python_deps() {
    info "Installing Python dependencies..."
    as_app "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
    as_app "$APP_DIR/.venv/bin/pip" install --quiet "$APP_DIR"
    info "Python dependencies up to date."
}

# ---------------------------------------------------------------------------
# 3. Run database migrations
# ---------------------------------------------------------------------------
run_migrations() {
    info "Running database migrations..."
    as_app bash "$APP_DIR/scripts/run-migrations.sh"
    info "Migrations complete."
}

# ---------------------------------------------------------------------------
# 4. Build frontend (only when frontend source changed)
# ---------------------------------------------------------------------------
frontend_changed() {
    # Returns true if any file under frontend/ changed in the last pull.
    local head before_pull
    head=$(as_app git -C "$APP_DIR" rev-parse HEAD)
    before_pull=$(as_app git -C "$APP_DIR" rev-parse HEAD@{1} 2>/dev/null || echo "")
    [[ -z "$before_pull" ]] && return 0  # First deploy; always build.
    as_app git -C "$APP_DIR" diff --quiet "${before_pull}..${head}" -- frontend/ \
        && return 1 || return 0
}

build_frontend() {
    info "Building React frontend..."
    # Use the systemd one-shot unit so the build runs under the right user and
    # environment.  If not running under systemd (e.g., CI), fall back to direct npm.
    if systemctl is-active --quiet sunday-voice-api 2>/dev/null || \
       systemctl is-enabled --quiet sunday-voice-frontend-build 2>/dev/null; then
        systemctl start sunday-voice-frontend-build
    else
        as_app bash -c "cd '$APP_DIR/frontend' && HOME='$APP_DIR' npm ci --prefer-offline && npm run build"
    fi
    info "Frontend build complete."
}

# ---------------------------------------------------------------------------
# 5. Restart application service
# ---------------------------------------------------------------------------
restart_app() {
    info "Restarting sunday-voice-api..."
    systemctl restart sunday-voice-api
    # Brief wait then confirm the service came up.
    sleep 2
    if systemctl is-active --quiet sunday-voice-api; then
        info "sunday-voice-api is running."
    else
        die "sunday-voice-api failed to start. Check: journalctl -u sunday-voice-api -n 50"
    fi
}

# ---------------------------------------------------------------------------
# 6. Reload Caddy if the Caddyfile changed
# ---------------------------------------------------------------------------
reload_caddy() {
    local deployed_cf="/etc/caddy/Caddyfile"
    local repo_cf="$APP_DIR/deploy/Caddyfile"
    if ! diff -q "$deployed_cf" "$repo_cf" &>/dev/null; then
        info "Caddyfile changed – updating and reloading Caddy..."
        cp "$repo_cf" "$deployed_cf"
        caddy validate --config "$deployed_cf" \
            || die "New Caddyfile is invalid. Aborting Caddy reload."
        systemctl reload caddy
        info "Caddy reloaded."
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    require_root

    pull_code
    install_python_deps
    run_migrations

    if frontend_changed; then
        build_frontend
    else
        info "No frontend changes detected – skipping build."
    fi

    restart_app
    reload_caddy

    info "======================================================"
    info "Deploy complete."
    info "  Commit : $(as_app git -C "$APP_DIR" rev-parse --short HEAD)"
    info "  Service: $(systemctl is-active sunday-voice-api)"
    info "  Logs   : journalctl -u sunday-voice-api -f"
    info "======================================================"
}

main "$@"
