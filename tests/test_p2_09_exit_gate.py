from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tools.build_interface import IMAGE_TARGETS
from tools.quality.phase2_exit_gate import (
    IMAGE_SPECS,
    ImageSpec,
    validate_image_record,
    validate_source_contract,
)

ROOT = Path(__file__).resolve().parents[1]


def test_p2_09_matrix_covers_the_declared_bake_targets() -> None:
    assert tuple(spec.profile for spec in IMAGE_SPECS) == IMAGE_TARGETS
    assert len(IMAGE_SPECS) == 8
    assert IMAGE_SPECS[0].role == "controller"
    assert all(spec.role == "worker" for spec in IMAGE_SPECS[1:])


def test_p2_09_source_contract_passes() -> None:
    assert validate_source_contract(ROOT) == []


def test_p2_09_forgejo_confines_docker_to_dedicated_builder() -> None:
    workflow = (ROOT / ".forgejo/workflows/ci.yml").read_text(encoding="utf-8")
    python_job, image_job = workflow.split("  images:\n", maxsplit=1)

    assert "runs-on: [docker, victus-fast]" in python_job
    assert "make check" in python_job
    assert "bootstrap_docker.sh" not in python_job
    assert "needs: python" in image_job
    assert "runs-on: [docker, victus-builder]" in image_job
    assert "bash ./tools/ci/bootstrap_docker.sh" in image_job
    assert "make phase2-images" in image_job
    assert image_job.index("bootstrap_docker.sh") < image_job.index("Install Python tooling")

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "phase2-gate: check\n\t$(MAKE) phase2-images" in makefile
    assert "phase2-images:\n\t$(MAKE) images" in makefile

    bootstrap = (ROOT / "tools/ci/bootstrap_docker.sh").read_text(encoding="utf-8")
    assert "docker-ce-cli docker-buildx-plugin" in bootstrap
    assert "docker-ce docker-ce-cli" not in bootstrap
    assert "containerd.io" not in bootstrap
    assert "start_ephemeral_daemon" not in bootstrap
    assert "dockerd \\" not in bootstrap
    assert "runner.envs.DOCKER_HOST" in bootstrap
    bake = (ROOT / "docker-bake.hcl").read_text(encoding="utf-8")
    assert "SEASONALWEATHER_DOCKER_BUILD_NETWORK" not in bake


def test_p2_09_forgejo_docker_preflight_accepts_runner_endpoint(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker.chmod(0o755)

    completed = subprocess.run(
        ["bash", str(ROOT / "tools/ci/bootstrap_docker.sh")],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "runner-provided endpoint and Buildx are ready" in completed.stdout


def test_p2_09_forgejo_docker_preflight_rejects_missing_endpoint(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/sh\n[ "$1" = "info" ] && exit 1\nexit 0\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)

    completed = subprocess.run(
        ["bash", str(ROOT / "tools/ci/bootstrap_docker.sh")],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "did not provide a usable Docker endpoint" in completed.stderr
    assert "docs/forgejo-runner-docker.md" in completed.stderr


def test_p2_09_github_keeps_the_native_docker_path() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "make phase2-gate" in workflow
    assert "bootstrap_docker.sh" not in workflow


def test_p2_09_image_record_rejects_worker_port_and_wrong_identity() -> None:
    spec = ImageSpec(
        profile="maintenance",
        tag="seasonalweather-worker:maintenance",
        role="worker",
        entrypoint=("python", "-m", "seasonalweather", "worker"),
        healthcheck=("CMD", "python", "-m", "seasonalweather", "health", "worker", "--mode", "liveness"),
    )
    info = {
        "project": "seasonalweather",
        "software_version": "0.18.0",
        "build_identity": "seasonalweather-0.18.0-0123456789ab",
        "git_commit": "0123456789abcdef0123456789abcdef01234567",
        "git_describe": "v0.18.0-1-g0123456",
        "dirty_tree": False,
        "build_source_timestamp": "2026-08-21T00:00:00Z",
        "source_date_epoch": 1_755_734_400,
        "image_profile": "controller",
        "build_id": "bld-test",
        "target_platform": "linux/amd64",
        "python_version": "3.11.13",
        "swwp_protocol_versions": [1],
        "job_payload_schema_versions": [1],
        "job_result_schema_versions": [1],
        "validation_protocol_versions": [1],
        "configuration_schema": {"minimum": 1, "maximum": 1},
        "diagnostic_schema_version": 1,
        "diagnostic_catalog_version": 1,
        "capability_manifest_version": 1,
    }
    inspect = {
        "Config": {
            "User": "seasonalweather",
            "Entrypoint": list(spec.entrypoint),
            "Healthcheck": {"Test": list(spec.healthcheck)},
            "ExposedPorts": {"9080/tcp": {}},
            "Labels": {},
        }
    }
    errors = validate_image_record(spec, inspect, info, {})
    assert any("profile" in error for error in errors)
    assert any("port" in error for error in errors)
