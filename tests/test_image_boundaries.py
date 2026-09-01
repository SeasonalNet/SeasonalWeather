from __future__ import annotations

import json
import tomllib
from pathlib import Path

from seasonalweather.diagnostics.exporter import export_catalog
from seasonalweather.diagnostics.loader import load_catalog
from tools.quality.image_boundaries_check import main

ROOT = Path(__file__).resolve().parents[1]


def test_controller_image_boundary_check_passes() -> None:
    assert main() == 0


def test_controller_lock_excludes_worker_execution_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = "\n".join(project["dependency-groups"]["controller"]).lower()

    for forbidden in ("piper-tts", "ffmpeg", "samedec", "samegen", "espeak", "legacy-tts"):
        assert forbidden not in lock


def test_controller_dockerfile_rejects_worker_profiles() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert 'RUN test "${SW_IMAGE_PROFILE}" = "controller"' in dockerfile
    assert "USER seasonalweather" in dockerfile


def test_controller_dockerfile_builds_and_carries_native_same_tools() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    bake = (ROOT / "docker-bake.hcl").read_text(encoding="utf-8")

    assert "FROM ${RUST_IMAGE} AS same-tools" in dockerfile
    assert "cargo build --locked --manifest-path /build/samegen/Cargo.toml --release" in dockerfile
    assert 'cargo install --locked --root /tmp/samedec-root --version "${SAMEDEC_VERSION}" samedec' in dockerfile
    assert "COPY --from=same-tools /out/usr/local/bin/samegen /usr/local/bin/samegen" in dockerfile
    assert "COPY --from=same-tools /out/usr/local/bin/samedec /usr/local/bin/samedec" in dockerfile
    assert 'variable "SAMEDEC_VERSION" { default = "0.4.2" }' in bake
    assert "SAMEDEC_VERSION = SAMEDEC_VERSION" in bake
    assert 'args = { SW_IMAGE_PROFILE = "controller" }' in bake
    assert '"io.seasonalweather.build.profile" = "controller"' in bake


def test_controller_dockerfile_removes_local_tts_implementation() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert '"tts/local.py"' in dockerfile
    assert '"tts/voicetext_paul_vtml.py"' in dockerfile
    assert "unlink(missing_ok=True)" in dockerfile


def test_worker_dockerfile_owns_only_worker_profiles() -> None:
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8").lower()

    for profile in (
        "routine-worker",
        "piper",
        "espeak",
        "festival",
        "dectalk",
        "legacy-tts",
        "voicetext-paul",
        "spfy",
        "maintenance",
        "development",
    ):
        assert profile in dockerfile
    assert 'entrypoint ["python", "-m", "seasonalweather", "worker"]' in dockerfile
    assert "expose" not in dockerfile
    for forbidden in ("requirements-controller.txt", "slixmpp", "sqlalchemy", "fastapi", "uvicorn"):
        assert forbidden not in dockerfile


def test_specialized_worker_profiles_carry_their_runtime_engines() -> None:
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")

    assert "uv sync --frozen" in dockerfile
    assert "VOICETEXT_ARCHIVE_SHA256" in dockerfile
    assert "SPEECHIFY_RELEASE_SHA256" in dockerfile
    assert "/var/lib/seasonalweather/voices/voicetext_paul/WeatherRadioSuite-LIB" in dockerfile
    assert "/opt/spfy/bin/spfy_synth" in dockerfile
    assert "wine32:i386" in dockerfile
    assert "xvfb" in dockerfile.lower()
    assert "docker/spfy/voice-manifest.txt" in dockerfile
    assert "DECTALK_SOURCE_SHA256" in dockerfile
    assert "/opt/dectalk/dectalk/dist/say" in dockerfile
    assert "scripts/wrappers/dectalk-text2wav" in dockerfile
    assert "apt-get purge --yes curl tar" not in dockerfile
    assert "apt-get purge --yes curl;" in dockerfile


def test_worker_image_retains_shared_artifact_runtime() -> None:
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    prune_start = dockerfile.index("package_root =")
    prune_end = dockerfile.index("RUN mkdir -p /usr/share/seasonalweather/diagnostics", prune_start)
    assert '"artifacts"' not in dockerfile[prune_start:prune_end]
    assert "from ..artifacts" in (ROOT / "seasonalweather/worker/handlers.py").read_text(encoding="utf-8")


def test_worker_dependency_locks_exclude_controller_runtime() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = [
        *project["project"]["dependencies"],
        *project["dependency-groups"]["worker-runtime"],
        *project["dependency-groups"]["piper"],
    ]
    lock = "\n".join(dependencies).lower()
    for forbidden in ("slixmpp", "sqlalchemy", "fastapi", "uvicorn"):
        assert forbidden not in lock


def test_controller_build_context_excludes_secrets_and_worker_locks() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    for excluded in (".git", ".venv", "*.env"):
        assert excluded in dockerignore


def test_controller_diagnostic_export_contains_the_complete_catalog(tmp_path: Path) -> None:
    destination = tmp_path / "usr/share/seasonalweather/diagnostics"
    _ = export_catalog(destination)
    exported = json.loads((destination / "catalog.json").read_text(encoding="utf-8"))
    catalog = load_catalog()

    assert {item["code"] for item in exported["definitions"]} == {str(item.code) for item in catalog.definitions}
    assert {path.name for path in (destination / "explanations").glob("*.md")} == {
        Path(item.explanation_path).name for item in catalog.definitions
    }
