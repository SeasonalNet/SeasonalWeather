# syntax=docker/dockerfile:1

ARG UV_VERSION=0.12.4
ARG PYTHON_BASE=python:3.11-slim-bookworm
ARG RUST_IMAGE=rust:1.85-bookworm
ARG SAMEDEC_VERSION=0.4.2

FROM ${RUST_IMAGE} AS same-tools

ARG SAMEDEC_VERSION

WORKDIR /build

# Build both native SAME tools into a small copy-only staging tree. samegen is
# repository-owned; samedec is consumed at an explicitly pinned crates.io
# release. The controller is the only image that needs these binaries.
COPY tools/samegen/Cargo.toml tools/samegen/Cargo.lock ./samegen/
COPY tools/samegen/src ./samegen/src
RUN cargo build --locked --manifest-path /build/samegen/Cargo.toml --release \
    && install -D -m 0755 /build/samegen/target/release/samegen /out/usr/local/bin/samegen \
    && cargo install --locked --root /tmp/samedec-root --version "${SAMEDEC_VERSION}" samedec \
    && install -D -m 0755 /tmp/samedec-root/bin/samedec /out/usr/local/bin/samedec

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM ${PYTHON_BASE} AS builder

ARG SW_PROJECT
ARG SW_VERSION
ARG SW_BUILD_ID
ARG SW_BUILD_IDENTITY
ARG SW_GIT_COMMIT
ARG SW_GIT_DESCRIBE
ARG SW_DIRTY_TREE
ARG SW_BUILD_SOURCE_TIMESTAMP
ARG SW_SOURCE_DATE_EPOCH
ARG SW_IMAGE_PROFILE
ARG SW_TARGET_PLATFORM
ARG SW_PYTHON_VERSION
ARG SW_SWWP_PROTOCOL_VERSIONS
ARG SW_JOB_PAYLOAD_SCHEMA_VERSIONS
ARG SW_JOB_RESULT_SCHEMA_VERSIONS
ARG SW_VALIDATION_PROTOCOL_VERSIONS
ARG SW_CONFIG_SCHEMA_MIN
ARG SW_CONFIG_SCHEMA_MAX
ARG SW_DIAGNOSTIC_SCHEMA_VERSION
ARG SW_DIAGNOSTIC_CATALOG_VERSION
ARG SW_CAPABILITY_MANIFEST_VERSION

RUN test "${SW_IMAGE_PROFILE}" = "controller"

ENV PATH="/opt/venv/bin:${PATH}" \
    UV_PROJECT_ENVIRONMENT="/opt/venv" \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SEASONALWEATHER="${SW_VERSION}" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SW_PROJECT="${SW_PROJECT}" \
    SW_VERSION="${SW_VERSION}" \
    SW_BUILD_ID="${SW_BUILD_ID}" \
    SW_BUILD_IDENTITY="${SW_BUILD_IDENTITY}" \
    SW_GIT_COMMIT="${SW_GIT_COMMIT}" \
    SW_GIT_DESCRIBE="${SW_GIT_DESCRIBE}" \
    SW_DIRTY_TREE="${SW_DIRTY_TREE}" \
    SW_BUILD_SOURCE_TIMESTAMP="${SW_BUILD_SOURCE_TIMESTAMP}" \
    SW_SOURCE_DATE_EPOCH="${SW_SOURCE_DATE_EPOCH}" \
    SW_IMAGE_PROFILE="${SW_IMAGE_PROFILE}" \
    SW_TARGET_PLATFORM="${SW_TARGET_PLATFORM}" \
    SW_PYTHON_VERSION="${SW_PYTHON_VERSION}" \
    SW_SWWP_PROTOCOL_VERSIONS="${SW_SWWP_PROTOCOL_VERSIONS}" \
    SW_JOB_PAYLOAD_SCHEMA_VERSIONS="${SW_JOB_PAYLOAD_SCHEMA_VERSIONS}" \
    SW_JOB_RESULT_SCHEMA_VERSIONS="${SW_JOB_RESULT_SCHEMA_VERSIONS}" \
    SW_VALIDATION_PROTOCOL_VERSIONS="${SW_VALIDATION_PROTOCOL_VERSIONS}" \
    SW_CONFIG_SCHEMA_MIN="${SW_CONFIG_SCHEMA_MIN}" \
    SW_CONFIG_SCHEMA_MAX="${SW_CONFIG_SCHEMA_MAX}" \
    SW_DIAGNOSTIC_SCHEMA_VERSION="${SW_DIAGNOSTIC_SCHEMA_VERSION}" \
    SW_DIAGNOSTIC_CATALOG_VERSION="${SW_DIAGNOSTIC_CATALOG_VERSION}" \
    SW_CAPABILITY_MANIFEST_VERSION="${SW_CAPABILITY_MANIFEST_VERSION}"

WORKDIR /build
RUN python -m venv /opt/venv

COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --group controller --no-install-project

COPY seasonalweather ./seasonalweather
RUN uv pip install --python /opt/venv/bin/python --no-deps .

# Keep the controller artifact safe when later worker packages are added to the
# shared source tree. P2-03 owns those packages and their image profiles.
RUN python - <<'PY'
import shutil
import sysconfig
from pathlib import Path

package_root = Path(sysconfig.get_paths()["purelib"]) / "seasonalweather"
for worker_root in ("worker", "workers"):
    shutil.rmtree(package_root / worker_root, ignore_errors=True)

# Local engine handlers are worker-image authorities. Keeping their source in
# the controller image would leave an avoidable bypass even though the
# controller dependency profile does not install the engine runtimes.
for relative_path in ("tts/local.py", "tts/voicetext_paul_vtml.py"):
    (package_root / relative_path).unlink(missing_ok=True)
PY

RUN mkdir -p /usr/share/seasonalweather/diagnostics \
    && seasonalweather diagnostics export --output /usr/share/seasonalweather/diagnostics

RUN mkdir -p /usr/share/seasonalweather \
    && python - <<'PY'
import os
from pathlib import Path

from seasonalweather.build_metadata import BuildInfo


def versions(name: str) -> tuple[int, ...]:
    raw = os.environ[name]
    return tuple(int(item) for item in raw.split(",") if item)


def optional_epoch() -> int | None:
    raw = os.environ["SW_SOURCE_DATE_EPOCH"]
    return int(raw) if raw else None


info = BuildInfo(
    schema_version=1,
    project=os.environ["SW_PROJECT"],
    software_version=os.environ["SW_VERSION"],
    git_commit=os.environ["SW_GIT_COMMIT"],
    git_describe=os.environ["SW_GIT_DESCRIBE"],
    dirty_tree=os.environ["SW_DIRTY_TREE"].lower() == "true",
    build_source_timestamp=os.environ["SW_BUILD_SOURCE_TIMESTAMP"] or None,
    source_date_epoch=optional_epoch(),
    build_id=os.environ["SW_BUILD_ID"],
    image_profile=os.environ["SW_IMAGE_PROFILE"],
    target_platform=os.environ["SW_TARGET_PLATFORM"],
    python_version=os.environ["SW_PYTHON_VERSION"],
    swwp_protocol_versions=versions("SW_SWWP_PROTOCOL_VERSIONS"),
    job_payload_schema_versions=versions("SW_JOB_PAYLOAD_SCHEMA_VERSIONS"),
    job_result_schema_versions=versions("SW_JOB_RESULT_SCHEMA_VERSIONS"),
    validation_protocol_versions=versions("SW_VALIDATION_PROTOCOL_VERSIONS"),
    configuration_schema=(int(os.environ["SW_CONFIG_SCHEMA_MIN"]), int(os.environ["SW_CONFIG_SCHEMA_MAX"])),
    diagnostic_schema_version=int(os.environ["SW_DIAGNOSTIC_SCHEMA_VERSION"]),
    diagnostic_catalog_version=int(os.environ["SW_DIAGNOSTIC_CATALOG_VERSION"]),
    capability_manifest_version=int(os.environ["SW_CAPABILITY_MANIFEST_VERSION"]),
)
Path("/usr/share/seasonalweather/build-info.json").write_text(info.to_json() + "\n", encoding="utf-8")
PY

FROM ${PYTHON_BASE} AS controller

ARG SW_IMAGE_PROFILE
RUN test "${SW_IMAGE_PROFILE}" = "controller"

LABEL io.seasonalweather.security.profile="controller" \
      io.seasonalweather.security.user="seasonalweather:10001:10001" \
      io.seasonalweather.security.read-only-root="required" \
      io.seasonalweather.security.no-new-privileges="required" \
      io.seasonalweather.security.cap-drop="ALL" \
      io.seasonalweather.security.tmpfs="/tmp,/run" \
      io.seasonalweather.security.secrets="read-only-per-service"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/share/seasonalweather /usr/share/seasonalweather
COPY --from=same-tools /out/usr/local/bin/samegen /usr/local/bin/samegen
COPY --from=same-tools /out/usr/local/bin/samedec /usr/local/bin/samedec

RUN groupadd --gid 10001 seasonalweather \
    && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --no-create-home \
        --shell /usr/sbin/nologin seasonalweather \
    && install -d -o 10001 -g 10001 /var/lib/seasonalweather/state /var/lib/seasonalweather/jobs \
        /var/lib/seasonalweather/artifacts /var/lib/seasonalweather/artifacts/audio \
        /var/lib/seasonalweather/artifacts/worker-artifacts/staging \
        /var/lib/seasonalweather /var/log/seasonalweather /run/seasonalweather \
    && find /usr/share/seasonalweather -type d -exec chmod a+rx {} + \
    && find /usr/share/seasonalweather -type f -exec chmod a+r {} +

WORKDIR /opt/seasonalweather
USER seasonalweather

EXPOSE 9080
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-m", "seasonalweather", "health", "controller", "--mode", "readiness"]
ENTRYPOINT ["python", "-m", "seasonalweather.api.server"]
