#!/usr/bin/env bash
# scripts/install.sh – Bootstrap a fresh Ubuntu LXC for Sunday Voice.
#
# Run as root on the target machine:
#   bash scripts/install.sh
#
# What this script does:
#   1. Install system packages (Python 3.12, PostgreSQL, Redis, Caddy, Node LTS, git).
#   2. Create the 'sundayvoice' system user.
#   3. Clone the repo to /opt/sunday-voice (or pull if already present).
#   4. Create a Python virtual environment and install backend dependencies.
#   5. Create the PostgreSQL role and database.
#   6. Create /opt/sunday-voice/.env from .env.example (if absent).
#   7. Install systemd units and enable services.
#   8. Print next-step instructions.
#
# The script does NOT start sunday-voice.service because .env must be
# configured with real secrets first.  See the printed instructions at the end.
set -euo pipefail

# ---------------------------------------------------------------------------
# Config (override via environment variables before running)
# ---------------------------------------------------------------------------
REPO="${REPO:-https://github.com/kalebhall/sunday-voice.git}"
APP_DIR="${APP_DIR:-/opt/sunday-voice}"
APP_USER="${APP_USER:-sundayvoice}"
APP_BRANCH="${APP_BRANCH:-main}"
DB_NAME="${DB_NAME:-sundayvoice}"
DB_USER="${DB_USER:-sundayvoice}"
# DB_PASS is generated randomly if not set.
DB_PASS="${DB_PASS:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { printf '\e[1;32m[install]\e[0m %s\n' "$*"; }
warn()  { printf '\e[1;33m[install]\e[0m %s\n' "$*" >&2; }
die()   { printf '\e[1;31m[install]\e[0m FATAL: %s\n' "$*" >&2; exit 1; }

require_root() {
    [[ "$(id -u)" -eq 0 ]] || die "This script must be run as root."
}

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
install_packages() {
    info "Updating apt and installing system packages..."
    export DEBIAN_FRONTEND=noninteractive

    apt-get update -qq

    # Python 3.12 (available in Ubuntu 24.04; add deadsnakes PPA for 22.04).
    if ! dpkg -s python3.12 &>/dev/null; then
        if grep -q "22.04" /etc/os-release 2>/dev/null; then
            info "Ubuntu 22.04 detected – adding deadsnakes PPA for Python 3.12."
            apt-get install -y -qq software-properties-common
            add-apt-repository -y ppa:deadsnakes/ppa
            apt-get update -qq
        fi
    fi

    apt-get install -y -qq \
        python3.12 \
        python3.12-venv \
        python3.12-dev \
        python3-pip \
        git \
        curl \
        ca-certificates \
        gnupg \
        lsb-release \
        postgresql \
        postgresql-client \
        redis-server \
        build-essential \
        libpq-dev

    install_caddy
    install_nodejs
}

install_caddy() {
    if command -v caddy &>/dev/null; then
        info "Caddy already installed ($(caddy version | head -1))."
        return
    fi
    info "Installing Caddy from official apt repository..."
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
}

install_nodejs() {
    if command -v node &>/dev/null; then
        info "Node.js already installed ($(node --version))."
        return
    fi
    info "Installing Node.js LTS via NodeSource..."
    NODE_MAJOR=22  # LTS 'Jod' as of 2025; bump to the next LTS when available.
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
    apt-get install -y -qq nodejs
    info "Node.js $(node --version) installed."
}

# ---------------------------------------------------------------------------
# 2. Application user
# ---------------------------------------------------------------------------
create_app_user() {
    if id "$APP_USER" &>/dev/null; then
        info "System user '$APP_USER' already exists."
    else
        info "Creating system user '$APP_USER'..."
        useradd --system --shell /usr/sbin/nologin \
            --home-dir "$APP_DIR" --create-home \
            "$APP_USER"
    fi
}

# ---------------------------------------------------------------------------
# 3. Clone / update repository
# ---------------------------------------------------------------------------
clone_repo() {
    # Git 2.35.2+ rejects operations on directories owned by a different user.
    # Register the app dir as safe at the system level so both root and
    # APP_USER can run git commands there without the dubious-ownership error.
    git config --system --add safe.directory "$APP_DIR"

    if [[ -d "$APP_DIR/.git" ]]; then
        info "Repository already present – fetching latest on branch '$APP_BRANCH'..."
        sudo -u "$APP_USER" git -C "$APP_DIR" fetch origin
        sudo -u "$APP_USER" git -C "$APP_DIR" checkout "$APP_BRANCH"
        sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard "origin/$APP_BRANCH"
    else
        info "Cloning repository to $APP_DIR..."
        # Ensure parent dir is accessible.
        install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
        sudo -u "$APP_USER" git clone --branch "$APP_BRANCH" "$REPO" "$APP_DIR"
    fi
}

# ---------------------------------------------------------------------------
# 4. Python virtual environment
# ---------------------------------------------------------------------------
setup_venv() {
    info "Creating Python virtual environment..."
    sudo -u "$APP_USER" python3.12 -m venv "$APP_DIR/.venv"
    sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
    sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet "$APP_DIR"
    info "Python dependencies installed."
}

# ---------------------------------------------------------------------------
# 5. Runtime directories
# ---------------------------------------------------------------------------
setup_dirs() {
    info "Creating runtime directories..."
    install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$APP_DIR/var"
    install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$APP_DIR/var/tts-cache"
}

# ---------------------------------------------------------------------------
# 6. PostgreSQL role and database
# ---------------------------------------------------------------------------
setup_postgres() {
    info "Configuring PostgreSQL..."
    systemctl enable --now postgresql

    # Create role (idempotent).
    if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
        info "PostgreSQL role '$DB_USER' already exists."
    else
        sudo -u postgres psql -c \
            "CREATE ROLE $DB_USER WITH LOGIN PASSWORD '$DB_PASS';"
        info "Created PostgreSQL role '$DB_USER'."
    fi

    # Create database (idempotent).
    if sudo -u postgres psql -lqt | cut -d '|' -f1 | grep -qw "$DB_NAME"; then
        info "PostgreSQL database '$DB_NAME' already exists."
    else
        sudo -u postgres psql -c \
            "CREATE DATABASE $DB_NAME OWNER $DB_USER;"
        info "Created PostgreSQL database '$DB_NAME'."
    fi
}

# ---------------------------------------------------------------------------
# 7. Environment file
# ---------------------------------------------------------------------------
setup_env() {
    local env_file="$APP_DIR/.env"
    if [[ -f "$env_file" ]]; then
        info ".env already exists – skipping."
        return
    fi
    info "Creating .env from .env.example..."
    cp "$APP_DIR/.env.example" "$env_file"

    # Substitute the generated DB password and production defaults.
    sed -i \
        -e "s|sundayvoice:sundayvoice@localhost|${DB_USER}:${DB_PASS}@localhost|g" \
        -e "s|APP_ENV=development|APP_ENV=production|g" \
        -e "s|APP_BASE_URL=http://localhost:8000|APP_BASE_URL=https://translate.thestand.app|g" \
        -e "s|APP_CORS_ORIGINS=http://localhost:5173|APP_CORS_ORIGINS=https://translate.thestand.app|g" \
        "$env_file"

    # Generate a strong SECRET_KEY.
    local secret
    secret=$(python3.12 -c 'import secrets; print(secrets.token_urlsafe(48))')
    sed -i "s|SECRET_KEY=change-me-to-a-long-random-string|SECRET_KEY=${secret}|g" "$env_file"

    chown "$APP_USER:$APP_USER" "$env_file"
    chmod 600 "$env_file"

    warn "--------------------------------------------------------------"
    warn ".env created at $env_file"
    warn "You MUST fill in the following before starting the app:"
    warn "  OPENAI_API_KEY"
    warn "  GOOGLE_APPLICATION_CREDENTIALS (path to service-account JSON)"
    warn "  GOOGLE_CLOUD_PROJECT"
    warn "--------------------------------------------------------------"
}

# ---------------------------------------------------------------------------
# 8. Systemd units
# ---------------------------------------------------------------------------
install_systemd_units() {
    info "Installing systemd units..."
    local unit_src="$APP_DIR/deploy/systemd"
    local unit_dst="/etc/systemd/system"

    cp "$unit_src/sunday-voice-api.service"           "$unit_dst/"
    cp "$unit_src/sunday-voice-cleanup.service"       "$unit_dst/"
    cp "$unit_src/sunday-voice-cleanup.timer"         "$unit_dst/"
    cp "$unit_src/sunday-voice-frontend-build.service" "$unit_dst/"

    systemctl daemon-reload

    # Enable the timer (cleanup runs hourly).
    systemctl enable sunday-voice-cleanup.timer
    systemctl start  sunday-voice-cleanup.timer

    # Enable the API service but do NOT start it yet (needs .env).
    systemctl enable sunday-voice-api.service

    info "Systemd units installed."
}

# ---------------------------------------------------------------------------
# 9. Caddy config
# ---------------------------------------------------------------------------
install_caddy_config() {
    info "Installing Caddyfile..."
    cp "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
    systemctl enable --now caddy
    # Validate syntax.
    caddy validate --config /etc/caddy/Caddyfile || warn "Caddyfile validation failed – check /etc/caddy/Caddyfile."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    require_root

    install_packages
    create_app_user
    clone_repo
    setup_venv
    setup_dirs
    setup_postgres
    setup_env
    install_systemd_units
    install_caddy_config

    info "======================================================"
    info "Installation complete."
    info ""
    info "Next steps:"
    info "  1. Edit $APP_DIR/.env and fill in API keys."
    info "  2. Run database migrations:"
    info "       $APP_DIR/scripts/run-migrations.sh"
    info "  3. Build the frontend:"
    info "       systemctl start sunday-voice-frontend-build"
    info "  4. Start the API:"
    info "       systemctl start sunday-voice-api"
    info "  5. Reload Caddy if you changed the Caddyfile:"
    info "       systemctl reload caddy"
    info "  6. Check logs:"
    info "       journalctl -u sunday-voice-api -f"
    info "======================================================"
}

main "$@"
