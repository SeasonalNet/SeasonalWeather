#!/usr/bin/env bash

# Provision the Docker client, Buildx, and (when necessary) an ephemeral
# daemon for the Forgejo image gate.  This file is sourced by CI so that
# DOCKER_HOST and the temporary daemon lifetime cover the complete gate step.

set -euo pipefail

EPHEMERAL_DOCKER_STATE_DIR=
EPHEMERAL_DOCKER_PID=

docker_endpoint_ready() {
    command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

buildx_ready() {
    docker buildx version >/dev/null 2>&1
}

install_docker_packages() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Docker is unavailable and runtime installation requires a root job container." >&2
        return 1
    fi
    if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg >/dev/null 2>&1; then
        echo "Docker is unavailable; this fallback supports Debian-family job containers with apt-get." >&2
        return 1
    fi

    # Use Docker's documented repository so the fallback meets Forgejo
    # Runner v13's Docker >=25 requirement instead of depending on an old
    # distribution docker.io package.
    local distro_id distro suite arch key value
    while IFS='=' read -r key value; do
        value="${value#\"}"
        value="${value%\"}"
        case "$key" in
            ID) distro_id="$value" ;;
            VERSION_CODENAME) suite="$value" ;;
        esac
    done < /etc/os-release
    case "${distro_id:-}" in
        debian)
            distro=debian
            suite="${suite:-bookworm}"
            ;;
        ubuntu)
            distro=ubuntu
            suite="${suite:-jammy}"
            ;;
        *)
            echo "Docker fallback cannot identify a supported Debian-family distribution." >&2
            return 1
            ;;
    esac
    arch="$(dpkg --print-architecture)"

    apt-get update
    apt-get install -y --no-install-recommends ca-certificates curl gnupg
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${distro}/gpg" \
        | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    printf '%s\n' \
        "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${distro} ${suite} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update

    apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin
}

start_ephemeral_daemon() {
    if ! command -v dockerd >/dev/null 2>&1; then
        echo "Docker CLI is installed but dockerd is unavailable; cannot provide the image gate." >&2
        return 1
    fi
    if [ "$(id -u)" -ne 0 ]; then
        echo "dockerd fallback requires a privileged root job container; no usable Docker endpoint is available." >&2
        return 1
    fi

    local parent state_dir socket pid_file log_file
    parent="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
    state_dir="$(mktemp -d "${parent%/}/seasonalweather-docker.XXXXXX")"
    socket="${state_dir}/docker.sock"
    pid_file="${state_dir}/dockerd.pid"
    log_file="${state_dir}/dockerd.log"
    EPHEMERAL_DOCKER_STATE_DIR="$state_dir"

    cleanup_ephemeral_daemon() {
        if [ -n "${EPHEMERAL_DOCKER_PID:-}" ]; then
            kill "$EPHEMERAL_DOCKER_PID" 2>/dev/null || true
            wait "$EPHEMERAL_DOCKER_PID" 2>/dev/null || true
        fi
        if [ -n "${EPHEMERAL_DOCKER_STATE_DIR:-}" ]; then
            rm -rf "$EPHEMERAL_DOCKER_STATE_DIR"
        fi
    }
    trap cleanup_ephemeral_daemon EXIT

    dockerd \
        --host="unix://${socket}" \
        --data-root="${state_dir}/data" \
        --exec-root="${state_dir}/exec" \
        --pidfile="$pid_file" \
        >"$log_file" 2>&1 &
    EPHEMERAL_DOCKER_PID=$!

    export DOCKER_HOST="unix://${socket}"
    local attempt
    for attempt in $(seq 1 30); do
        if docker info >/dev/null 2>&1; then
            echo "Docker: started ephemeral daemon at ${DOCKER_HOST}"
            return 0
        fi
        if [ ! -f "$pid_file" ] || ! kill -0 "$(cat "$pid_file")" 2>/dev/null; then
            echo "Ephemeral dockerd exited before becoming ready:" >&2
            tail -40 "$log_file" >&2 || true
            return 1
        fi
        if [ "$attempt" -eq 30 ]; then
            echo "Timed out waiting for the ephemeral Docker daemon:" >&2
            tail -40 "$log_file" >&2 || true
            return 1
        fi
        sleep 1
    done
}

bootstrap_docker() {
    if docker_endpoint_ready && buildx_ready; then
        echo "Docker: using the existing Docker endpoint and Buildx"
        docker buildx inspect --bootstrap >/dev/null
        return 0
    fi

    if ! command -v docker >/dev/null 2>&1 || ! command -v dockerd >/dev/null 2>&1 || ! buildx_ready; then
        install_docker_packages
    fi

    if ! docker_endpoint_ready; then
        start_ephemeral_daemon
    fi
    if ! buildx_ready; then
        echo "Docker Buildx is unavailable after runtime provisioning." >&2
        return 1
    fi

    docker info >/dev/null
    docker buildx inspect --bootstrap >/dev/null
    echo "Docker: endpoint and Buildx are ready"
}

bootstrap_docker "$@"
