#!/usr/bin/env bash

# Install the Docker client and verify the runner-owned Docker endpoint used by
# the Forgejo image gate. The runner, not its unprivileged job container, owns
# daemon provisioning and isolation.

set -euo pipefail

docker_endpoint_ready() {
    command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

buildx_ready() {
    docker buildx version >/dev/null 2>&1
}

install_docker_client() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Installing the Docker client requires a root Forgejo job container." >&2
        return 1
    fi
    if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg >/dev/null 2>&1; then
        echo "Docker client setup supports Debian-family job containers with apt-get." >&2
        return 1
    fi

    # Use Docker's documented repository for a current CLI and Buildx plugin.
    # Do not install Docker Engine or containerd in the job container: nested
    # dockerd requires mount capabilities that the Forgejo Docker backend does
    # not grant by default.
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
            echo "Docker client setup cannot identify a supported Debian-family distribution." >&2
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

    apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin
}

bootstrap_docker() {
    if docker_endpoint_ready && buildx_ready; then
        docker buildx inspect --bootstrap >/dev/null
        echo "Docker: runner-provided endpoint and Buildx are ready"
        return 0
    fi

    if ! command -v docker >/dev/null 2>&1 || ! buildx_ready; then
        install_docker_client
    fi

    if ! docker_endpoint_ready; then
        cat >&2 <<'EOF'
Forgejo Runner did not provide a usable Docker endpoint.
P2-09 image validation requires runner-owned Docker access; an unprivileged
Docker-backend job container cannot safely start its own nested daemon.
Configure an isolated DIND endpoint through runner.envs.DOCKER_HOST and
container.docker_host as documented in docs/forgejo-runner-docker.md and at:
https://forgejo.org/docs/v15.0/admin/actions/docker-access/
EOF
        return 1
    fi
    if ! buildx_ready; then
        echo "Docker Buildx is unavailable after client installation." >&2
        return 1
    fi

    docker buildx inspect --bootstrap >/dev/null
    echo "Docker: runner-provided endpoint and Buildx are ready"
}

bootstrap_docker "$@"
