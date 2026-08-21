from __future__ import annotations

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
    assert len(IMAGE_SPECS) == 6
    assert IMAGE_SPECS[0].role == "controller"
    assert all(spec.role == "worker" for spec in IMAGE_SPECS[1:])


def test_p2_09_source_contract_passes() -> None:
    assert validate_source_contract(ROOT) == []


def test_p2_09_forgejo_bootstraps_docker_inside_the_gate_step() -> None:
    workflow = (ROOT / ".forgejo/workflows/ci.yml").read_text(encoding="utf-8")
    assert ". ./tools/ci/bootstrap_docker.sh" in workflow
    assert "make phase2-gate" in workflow
    assert workflow.index("bootstrap_docker.sh") < workflow.index("make phase2-gate")

    bootstrap = (ROOT / "tools/ci/bootstrap_docker.sh").read_text(encoding="utf-8")
    assert '--pidfile="$pid_file"' in bootstrap
    assert '--pid-file="$pid_file"' not in bootstrap
    assert "--iptables=false" in bootstrap
    assert "--ip6tables=false" in bootstrap
    assert "--ip-masq=false" in bootstrap
    assert "--ip-forward=false" in bootstrap
    assert "--bridge=none" in bootstrap
    assert "SEASONALWEATHER_DOCKER_BUILD_NETWORK=host" in bootstrap
    assert "SEASONALWEATHER_DOCKER_RUN_NETWORK=none" in bootstrap
    bake = (ROOT / "docker-bake.hcl").read_text(encoding="utf-8")
    assert 'variable "SEASONALWEATHER_DOCKER_BUILD_NETWORK"' in bake
    assert "network = SEASONALWEATHER_DOCKER_BUILD_NETWORK" in bake


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
