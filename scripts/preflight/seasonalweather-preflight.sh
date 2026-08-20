#!/usr/bin/env bash
set -euo pipefail

AUDIO="${SEASONALWEATHER_AUDIO_DIR:-/var/lib/seasonalweather/artifacts/audio}"
MIN_FREE_MB="${SEASONALWEATHER_MIN_FREE_MB:-512}"

free_mb() {
  df -Pm / | awk 'NR==2{print $4}'
}

if [[ ! -d "$AUDIO" ]]; then
  exit 0
fi

if [[ "$(free_mb)" -lt "$MIN_FREE_MB" ]]; then
  echo "[preflight] Low disk space: $(free_mb)MB free; cleaning generated WAVs..." >&2

  ls -1t "$AUDIO"/cycle_*.wav 2>/dev/null | tail -n +11 | xargs -r rm -f
  ls -1t "$AUDIO"/capvoice_*.wav 2>/dev/null | tail -n +21 | xargs -r rm -f
  rm -f "$AUDIO"/*.tmp.wav 2>/dev/null || true

  echo "[preflight] After cleanup: $(free_mb)MB free" >&2
fi
