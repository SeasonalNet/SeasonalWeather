from __future__ import annotations

import json
from pathlib import Path

from seasonalweather.diagnostics.exporter import export_catalog
from seasonalweather.diagnostics.loader import load_catalog
from tools.quality.image_boundaries_check import main

ROOT = Path(__file__).resolve().parents[1]


def test_controller_image_boundary_check_passes() -> None:
    assert main() == 0


def test_controller_lock_excludes_worker_execution_dependencies() -> None:
    lock = (ROOT / "requirements-controller.txt").read_text(encoding="utf-8").lower()

    for forbidden in ("piper", "ffmpeg", "samedec", "samegen", "espeak", "legacy-tts"):
        assert forbidden not in lock


def test_controller_dockerfile_rejects_worker_profiles() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert 'RUN test "${SW_IMAGE_PROFILE}" = "controller"' in dockerfile
    assert "USER seasonalweather" in dockerfile


def test_controller_build_context_excludes_secrets_and_worker_locks() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    for excluded in (".git", ".venv", "requirements-piper.txt", "*.env"):
        assert excluded in dockerignore


def test_controller_diagnostic_export_contains_the_complete_catalog(tmp_path: Path) -> None:
    destination = tmp_path / "usr/share/seasonalweather/diagnostics"
    export_catalog(destination)
    exported = json.loads((destination / "catalog.json").read_text(encoding="utf-8"))
    catalog = load_catalog()

    assert {item["code"] for item in exported["definitions"]} == {str(item.code) for item in catalog.definitions}
    assert {path.name for path in (destination / "explanations").glob("*.md")} == {
        Path(item.explanation_path).name for item in catalog.definitions
    }
