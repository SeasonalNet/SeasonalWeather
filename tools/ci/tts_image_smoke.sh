#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
smoke_script="${repo_root}/tools/ci/tts_engine_smoke.py"
spfy_image="${SEASONALWEATHER_SPFY_SMOKE_IMAGE:-seasonalweather-worker:spfy}"
dectalk_image="${SEASONALWEATHER_DECTALK_SMOKE_IMAGE:-seasonalweather-worker:dectalk}"
voicetext_image="${SEASONALWEATHER_VOICETEXT_SMOKE_IMAGE:-seasonalweather-worker:voicetext-paul}"

common=(
  --rm
  --interactive
  --read-only
  --network none
  --cap-drop ALL
  --security-opt no-new-privileges
  --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777
  --tmpfs /run:rw,nosuid,nodev,size=64m,mode=755
)

docker run "${common[@]}" \
  --entrypoint python \
  "${dectalk_image}" - --profile dectalk < "${smoke_script}"

docker run "${common[@]}" \
  --entrypoint python \
  "${spfy_image}" - --profile spfy < "${smoke_script}"

# Keep Xvfb's socket directory root-owned across Docker implementations.
docker run "${common[@]}" \
  --mount type=volume,dst=/var/lib/seasonalweather/voices/voicetext_paul \
  --tmpfs /var/lib/seasonalweather/wineprefixes:rw,nosuid,nodev,size=1g,mode=0700,uid=10001,gid=10001 \
  --tmpfs /tmp/.X11-unix:rw,nosuid,nodev,noexec,size=8m,mode=1777,uid=0,gid=0 \
  --env DISPLAY=:99 \
  --env HOME=/tmp/voicetext/home \
  --entrypoint /bin/bash \
  "${voicetext_image}" -ceu '
    mkdir -p /tmp/voicetext/home
    chmod 0700 /tmp/voicetext/home
    Xvfb :99 -screen 0 1024x768x24 -nolisten tcp -noreset -ac </dev/null &
    xvfb_pid=$!
    cleanup() {
      kill "${xvfb_pid}" 2>/dev/null || true
      wait "${xvfb_pid}" 2>/dev/null || true
    }
    trap cleanup EXIT INT TERM
    for _ in $(seq 1 50); do
      [[ -S /tmp/.X11-unix/X99 ]] && break
      sleep 0.1
    done
    [[ -S /tmp/.X11-unix/X99 ]]
    set +e
    python - --profile voicetext-paul
    status=$?
    set -e
    if [[ ${status} -ne 0 ]]; then
      for log in /tmp/voicetext/voicetext_paul_wine.*.log; do
        [[ -s "${log}" ]] || continue
        echo "[tts-image-smoke] ${log}" >&2
        tail -n 80 "${log}" >&2 || true
      done
    fi
    exit "${status}"
  ' < "${smoke_script}"
