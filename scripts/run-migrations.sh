#!/usr/bin/env bash
# Apply alembic migrations against the configured database.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
../.venv/bin/alembic upgrade head
