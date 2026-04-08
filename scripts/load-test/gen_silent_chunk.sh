#!/usr/bin/env bash
# Generate a ~2.5 second silent WebM/Opus audio chunk for load testing.
#
# Usage:  bash scripts/load-test/gen_silent_chunk.sh
#
# Requires ffmpeg with libopus support.  The output file is consumed by
# k6_load_test.js when USE_AUDIO=1 is set.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$SCRIPT_DIR/silent_chunk.webm"

if ! command -v ffmpeg &>/dev/null; then
    echo "ERROR: ffmpeg not found; install it with: sudo apt install ffmpeg" >&2
    exit 1
fi

ffmpeg -y \
    -f lavfi -i "anullsrc=r=16000:cl=mono" \
    -t 2.5 \
    -c:a libopus \
    -b:a 24k \
    -vbr on \
    -compression_level 10 \
    -f webm \
    "$OUT" 2>/dev/null

echo "Generated: $OUT ($(wc -c < "$OUT") bytes)"
