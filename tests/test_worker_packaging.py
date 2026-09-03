from __future__ import annotations

import ast
import datetime as dt
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from seasonalweather.build_metadata import current_build_info
from seasonalweather.build_metadata.compatibility import WORKER_BUILD_PROFILES, check_runtime_compatibility
from seasonalweather.tts.models import (
    BackendId,
    LocalEngineOptions,
    LocalQualification,
    LocalQualificationDisposition,
    SynthesisPurpose,
    SynthesisRequest,
)
from seasonalweather.worker.handlers import _assigned_local_capability
from seasonalweather.worker.profiles import WorkerProfile

ROOT = Path(__file__).resolve().parents[1]


def _docker_script(marker: str) -> str:
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    section = dockerfile[dockerfile.index(marker) :]
    return section.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def test_pruned_worker_package_imports_startup_and_local_synthesis(tmp_path: Path) -> None:
    package = tmp_path / "seasonalweather"
    shutil.copytree(ROOT / "seasonalweather", package, ignore=shutil.ignore_patterns("__pycache__"))
    prune = ast.parse(_docker_script("# Worker images do not carry"))
    paths: tuple[str, ...] = next(
        ast.literal_eval(node.iter)
        for node in prune.body
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == "name"
    )
    for name in paths:
        target = package / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)

    smoke = _docker_script("# Import the real worker startup")
    code = f"import sys\nsys.path.insert(0, {str(tmp_path)!r})\n" + smoke
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        env={**os.environ, "SW_IMAGE_PROFILE": "development"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_worker_build_smoke_cannot_import_the_unpruned_build_context() -> None:
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    section = dockerfile[dockerfile.index("# Import the real worker startup") :]
    assert "RUN python -I - <<'PY'" in section.split("\nPY\n", 1)[0]
    assert "RUN python -I -m seasonalweather worker --help" in dockerfile
    assert 'HOME="/tmp"' in dockerfile
    assert 'PIPER_MODEL_DIR="/opt/piper/models"' in dockerfile


def test_controller_authority_modules_are_not_shipped_in_workers() -> None:
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    for name in (
        "artifacts/service.py",
        "artifacts/integration.py",
        "artifacts/promotion.py",
        "artifacts/staging.py",
        "capabilities/service.py",
        "capabilities/registry.py",
        "jobs/worker_client.py",
        "swwp/adapter.py",
        "swwp/controller.py",
        "tts/adapters/remote.py",
        "tts/adapters/transport.py",
        "tts/admission.py",
    ):
        assert f'"{name}"' in dockerfile


def test_worker_execution_evidence_is_local_and_capability_specific() -> None:
    request = SynthesisRequest(
        purpose=SynthesisPurpose.ROUTINE,
        backend=BackendId.LOCAL,
        text="A worker packaging test.",
        deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30),
        local=LocalEngineOptions(engine="espeak-ng", voice="en"),
    )
    from seasonalweather.tts.local import LocalEngineRegistry

    capability = LocalEngineRegistry.capability_for("espeak-ng")
    accepted = _assigned_local_capability(request, capability)
    assert isinstance(accepted, LocalQualification)
    assert accepted.disposition is LocalQualificationDisposition.SATISFIED
    assert accepted.effective_capacity == 1
    for candidate, name in (
        (request, "tts.local.piper"),
        (request.model_copy(update={"backend": BackendId.SEASONAL_TTSD}), capability),
        (object(), capability),
    ):
        rejected = _assigned_local_capability(candidate, name)
        assert isinstance(rejected, LocalQualification)
        assert rejected.disposition is LocalQualificationDisposition.INCOMPATIBLE
        assert rejected.effective_capacity == 0


def test_controller_qualification_public_imports_remain_compatible() -> None:
    from seasonalweather.tts import admission, models, request_validation

    assert admission.LocalQualification is models.LocalQualification
    assert admission.LocalQualificationDisposition is models.LocalQualificationDisposition
    assert admission.validate_synthesis_request is request_validation.validate_synthesis_request


def test_every_worker_image_profile_passes_only_its_own_runtime_role() -> None:
    assert {profile.value for profile in WorkerProfile} | {"source"} == WORKER_BUILD_PROFILES
    for profile in WorkerProfile:
        info = replace(current_build_info(), image_profile=profile.value)
        assert check_runtime_compatibility(info, role="worker", expected_profile=profile.value).compatible
        assert not check_runtime_compatibility(info, role="controller").compatible
        assert not check_runtime_compatibility(info, role="worker", expected_profile="controller").compatible


def test_final_image_checks_embedded_build_compatibility() -> None:
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    final_checks = dockerfile.split("USER seasonalweather", 1)[1]
    assert "ensure_runtime_compatibility(" in final_checks
    assert 'expected_profile=os.environ["SW_IMAGE_PROFILE"]' in final_checks
