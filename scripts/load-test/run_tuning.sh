#!/usr/bin/env bash
# Sunday Voice — latency-tuning runner
#
# Iterates over combinations of:
#   - uvicorn worker count
#   - WHISPER_MAX_CONCURRENT
#   - WHISPER_CHUNK_FLUSH_BYTES
#
# For each combination it:
#   1. Updates /opt/sunday-voice/.env and the systemd service file.
#   2. Restarts the service and waits for readiness.
#   3. Runs the k6 load test in transcript-injection mode.
#   4. Captures p(95) of e2e_latency_ms and prints a results table.
#
# Usage
# -----
#   sudo bash scripts/load-test/run_tuning.sh
#
# Requirements
# ------------
#   - k6 in PATH  (https://k6.io/docs/get-started/installation/)
#   - jq in PATH  (apt install jq)
#   - Root / sudo to restart the systemd service
#   - Server running at BASE_URL with real Google Cloud credentials for translation
#
# Override defaults
# -----------------
#   BASE_URL=http://myserver:8000 \
#   ENV_FILE=/opt/sunday-voice/.env \
#   SERVICE=sunday-voice-api \
#   NUM_SESSIONS=5 \
#   LISTENERS_PER_SESSION=100 \
#   RAMP_UP_SECONDS=20 \
#   HOLD_SECONDS=60 \
#   bash scripts/load-test/run_tuning.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K6_SCRIPT="$SCRIPT_DIR/k6_load_test.js"

BASE_URL="${BASE_URL:-http://localhost:8000}"
ENV_FILE="${ENV_FILE:-/opt/sunday-voice/.env}"
SERVICE="${SERVICE:-sunday-voice-api}"
NUM_SESSIONS="${NUM_SESSIONS:-5}"
LISTENERS_PER_SESSION="${LISTENERS_PER_SESSION:-100}"
RAMP_UP_SECONDS="${RAMP_UP_SECONDS:-20}"
HOLD_SECONDS="${HOLD_SECONDS:-60}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-Sunday1234!}"

# Tuning matrix ----------------------------------------------------------
# Format: "workers:whisper_max_concurrent:flush_kb"
CONFIGS=(
  "1:5:32"    # single-worker baseline (in-process pub/sub fully connected)
  "2:5:32"    # 2 workers, semi-parallel listener fan-out
  "4:5:32"    # 4 workers, full listener fan-out parallelism
  "4:3:32"    # fewer concurrent Whisper calls (less API pressure)
  "4:8:32"    # more concurrent Whisper calls (if API allows)
  "4:5:16"    # smaller flush — more frequent Whisper calls, lower chunk latency
  "4:5:64"    # larger flush — batch two chunks, slightly lower API cost
)

# Results table
declare -a RESULTS

# ---------------------------------------------------------------------------
# Helper: set a key in the .env file; append if missing.
# ---------------------------------------------------------------------------
set_env_var() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

# ---------------------------------------------------------------------------
# Helper: wait up to 30 s for the server to report ready.
# ---------------------------------------------------------------------------
wait_ready() {
  local url="$BASE_URL/readyz"
  echo -n "  waiting for readyz"
  for i in $(seq 1 30); do
    if curl -sf "$url" &>/dev/null; then
      echo " ok (${i}s)"
      return 0
    fi
    echo -n "."
    sleep 1
  done
  echo " TIMEOUT"
  return 1
}

# ---------------------------------------------------------------------------
# Helper: run k6 and return p95 in milliseconds (integer).
# Returns 99999 on failure.
# ---------------------------------------------------------------------------
run_k6() {
  local workers="$1" max_concurrent="$2" flush_kb="$3"
  local label="${workers}w/${max_concurrent}wc/${flush_kb}kB"
  local tmp_out
  tmp_out="$(mktemp)"

  echo "  running k6 ($label)..."

  k6 run \
    --no-color \
    --env BASE_URL="$BASE_URL" \
    --env ADMIN_EMAIL="$ADMIN_EMAIL" \
    --env ADMIN_PASSWORD="$ADMIN_PASSWORD" \
    --env NUM_SESSIONS="$NUM_SESSIONS" \
    --env LISTENERS_PER_SESSION="$LISTENERS_PER_SESSION" \
    --env RAMP_UP_SECONDS="$RAMP_UP_SECONDS" \
    --env HOLD_SECONDS="$HOLD_SECONDS" \
    --env USE_AUDIO=0 \
    --summary-export "$tmp_out" \
    "$K6_SCRIPT" 2>&1 | tail -20 || true

  # Extract p95 from the JSON summary export.
  local p95="99999"
  if [[ -s "$tmp_out" ]]; then
    p95="$(jq -r '.metrics.e2e_latency_ms.values["p(95)"] // 99999' "$tmp_out" 2>/dev/null || echo 99999)"
    p95="${p95%.*}"  # truncate to integer ms
  fi
  rm -f "$tmp_out"
  echo "$p95"
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

echo "========================================================"
echo "  Sunday Voice — latency tuning sweep"
echo "  target: p(95) e2e_latency_ms ≤ 3000"
echo "========================================================"
echo ""
printf "%-30s  %12s  %8s\n" "config (workers/wc/flush)" "p95_ms" "pass?"
printf "%-30s  %12s  %8s\n" "------------------------------" "------------" "--------"

for cfg in "${CONFIGS[@]}"; do
  IFS=: read -r workers max_concurrent flush_kb <<< "$cfg"
  flush_bytes=$(( flush_kb * 1024 ))
  label="${workers}w / max_concurrent=${max_concurrent} / flush=${flush_kb}kB"

  echo ""
  echo "--- $label ---"

  # 1. Write new settings to .env
  set_env_var "WHISPER_MAX_CONCURRENT" "$max_concurrent"
  set_env_var "WHISPER_CHUNK_FLUSH_BYTES" "$flush_bytes"

  # 2. Update worker count in the systemd service ExecStart line.
  sudo sed -i "s/--workers [0-9]*/--workers ${workers}/" \
    /etc/systemd/system/sunday-voice-api.service
  sudo systemctl daemon-reload
  sudo systemctl restart "$SERVICE"

  # 3. Wait for the process to come up.
  if ! wait_ready; then
    RESULTS+=("$(printf '%-30s  %12s  %8s' "$label" "ERROR" "✗")")
    continue
  fi

  # 4. Run k6 and collect p95.
  p95="$(run_k6 "$workers" "$max_concurrent" "$flush_kb")"
  pass="✗"
  [[ "$p95" -le 3000 ]] && pass="✓"
  RESULTS+=("$(printf '%-30s  %12s  %8s' "$label" "${p95}ms" "$pass")")
  printf '%-30s  %12s  %8s\n' "$label" "${p95}ms" "$pass"
done

# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

echo ""
echo "========================================================"
echo "  RESULTS SUMMARY"
echo "========================================================"
printf "%-30s  %12s  %8s\n" "config (workers/wc/flush)" "p95_ms" "pass?"
printf "%-30s  %12s  %8s\n" "------------------------------" "------------" "--------"
for row in "${RESULTS[@]}"; do
  echo "$row"
done
echo ""
echo "Note: e2e_latency_ms measures translation + Redis + WebSocket delivery."
echo "Add server-side segment_transcription_duration_seconds p95 (from /metrics)"
echo "to get the full end-to-end figure including Whisper transcription time."
