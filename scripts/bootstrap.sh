#!/usr/bin/env bash
# Create a venv and install backend dependencies for local development.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -e ".[dev]"

echo "Done. Activate with: source .venv/bin/activate"
