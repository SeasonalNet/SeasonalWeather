from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import httpx2

from seasonalweather.api.api import create_app
from seasonalweather.api.auth import ApiPrincipal, get_api_principal
from seasonalweather.build_metadata import BuildInfo, collect_build_info
from seasonalweather.control import OrchestratorControl
from seasonalweather.swwp.messages import Register
from tools import build_interface
from tools.build_interface import IMAGE_TARGETS, _controlled_environment

ROOT = Path(__file__).resolve().parents[1]


def _info() -> BuildInfo:
    return collect_build_info(
        repo_root=ROOT,
        image_profile="controller",
        target_platform="linux/amd64",
        source_date_epoch=1_700_000_000,
        build_id="bld-test-fixed",
    )


def test_build_info_is_deterministic_for_controlled_inputs() -> None:
    first = _info()
    second = _info()

    assert first.to_json() == second.to_json()
    assert first.build_identity.endswith("-dirty") is first.dirty_tree
    assert first.to_dict()["build_identity"] == first.build_identity


def test_build_metadata_import_does_not_load_validation_pipeline() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import seasonalweather.build_metadata.build_info; "
                "assert 'seasonalweather.validation.pipeline' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert probe.returncode == 0, probe.stderr


def test_build_info_round_trips_and_projects_oci_labels() -> None:
    info = _info()
    restored = BuildInfo.from_dict(json.loads(info.to_json()))
    labels = restored.oci_labels()

    assert restored == info
    assert labels["org.opencontainers.image.version"] == info.software_version
    assert labels["io.seasonalweather.build.identity"] == info.build_identity
    assert labels["io.seasonalweather.build.dirty"] == ("true" if info.dirty_tree else "false")
    assert labels["io.seasonalweather.build.target-platform"] == "linux/amd64"


def test_build_info_rejects_contradictory_identity() -> None:
    payload = _info().to_dict()
    payload["build_identity"] = "wrong"

    try:
        BuildInfo.from_dict(payload)
    except ValueError as exc:
        assert "build_identity" in str(exc)
    else:
        raise AssertionError("contradictory build identity was accepted")


def test_bake_environment_contains_only_controlled_identity_inputs(monkeypatch) -> None:
    for key in build_interface.BUILD_CACHE_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    info = _info()
    environment = _controlled_environment(info)

    assert set(environment) == {
        "PATH",
        "SW_PROJECT",
        "SW_VERSION",
        "SW_BUILD_ID",
        "SW_BUILD_IDENTITY",
        "SW_GIT_COMMIT",
        "SW_GIT_DESCRIBE",
        "SW_DIRTY_TREE",
        "SW_BUILD_SOURCE_TIMESTAMP",
        "SW_SOURCE_DATE_EPOCH",
        "SW_IMAGE_PROFILE",
        "SW_TARGET_PLATFORM",
        "SW_PYTHON_VERSION",
        "SW_SWWP_PROTOCOL_VERSIONS",
        "SW_JOB_PAYLOAD_SCHEMA_VERSIONS",
        "SW_JOB_RESULT_SCHEMA_VERSIONS",
        "SW_VALIDATION_PROTOCOL_VERSIONS",
        "SW_CONFIG_SCHEMA_MIN",
        "SW_CONFIG_SCHEMA_MAX",
        "SW_DIAGNOSTIC_SCHEMA_VERSION",
        "SW_DIAGNOSTIC_CATALOG_VERSION",
        "SW_CAPABILITY_MANIFEST_VERSION",
    }
    assert "HOME" not in environment
    assert "SECRET_TOKEN" not in environment


def test_bake_environment_preserves_only_docker_transport_inputs() -> None:
    environment = _controlled_environment(
        _info(),
        docker_environment={
            "DOCKER_HOST": "unix:///tmp/docker.sock",
            "SECRET_TOKEN": "must-not-forward",
        },
    )

    assert environment["DOCKER_HOST"] == "unix:///tmp/docker.sock"
    assert "SECRET_TOKEN" not in environment


def test_bake_environment_preserves_only_github_cache_runtime_inputs(monkeypatch) -> None:
    monkeypatch.setenv("ACTIONS_CACHE_URL", "https://cache.example")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "runtime-token")
    environment = _controlled_environment(_info())

    assert environment["ACTIONS_CACHE_URL"] == "https://cache.example"
    assert environment["ACTIONS_RUNTIME_TOKEN"] == "runtime-token"
    assert "SECRET_TOKEN" not in environment


def test_build_interface_can_push_one_target_to_an_explicit_release_reference(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SW_BUILD_CACHE_FROM", raising=False)
    monkeypatch.delenv("SW_BUILD_CACHE_TO", raising=False)
    build_info = tmp_path / "build-info.json"
    build_info.write_text(_info().to_json(), encoding="utf-8")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_interface.subprocess, "run", fake_run)

    assert (
        build_interface.run_image(
            build_info=build_info,
            targets=("controller",),
            push=True,
            image_reference="ghcr.io/seasonalnet/seasonalweather:v0.18.0-alpha.2-controller",
        )
        == 0
    )
    assert calls[0][0] == [
        "docker",
        "buildx",
        "bake",
        "--file",
        str(build_interface.BAKE_FILE),
        "--push",
        "--set",
        "controller.tags=ghcr.io/seasonalnet/seasonalweather:v0.18.0-alpha.2-controller",
        "controller",
    ]


def test_build_interface_engine_push_loads_then_uses_docker_push(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SW_BUILD_CACHE_FROM", raising=False)
    monkeypatch.delenv("SW_BUILD_CACHE_TO", raising=False)
    monkeypatch.setenv("SW_IMAGE_PUSH_MODE", "engine")
    build_info = tmp_path / "build-info.json"
    build_info.write_text(_info().to_json(), encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_interface.subprocess, "run", fake_run)

    image_reference = "git.seasonalnet.org/seasonalnet/seasonalweather-worker:v0.18.0-alpha.3-voicetext-paul"
    assert (
        build_interface.run_image(
            build_info=build_info,
            targets=("voicetext-paul",),
            push=True,
            image_reference=image_reference,
        )
        == 0
    )

    assert calls == [
        [
            "docker",
            "buildx",
            "bake",
            "--file",
            str(build_interface.BAKE_FILE),
            "--load",
            "--set",
            f"voicetext-paul.tags={image_reference}",
            "voicetext-paul",
        ],
        ["docker", "push", image_reference],
    ]


def test_build_interface_scopes_optional_cache_references_per_profile(tmp_path, monkeypatch) -> None:
    build_info = tmp_path / "build-info.json"
    build_info.write_text(_info().to_json(), encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_interface.subprocess, "run", fake_run)
    monkeypatch.setenv(
        "SW_BUILD_CACHE_FROM",
        "type=registry,ref=registry.example/seasonalweather-cache:{profile}",
    )
    monkeypatch.setenv(
        "SW_BUILD_CACHE_TO", "type=registry,ref=registry.example/seasonalweather-cache:{profile},mode=max"
    )

    assert build_interface.run_image(build_info=build_info, targets=("controller",)) == 0

    assert calls[0][-5:] == [
        "--set",
        "controller.cache-from=type=registry,ref=registry.example/seasonalweather-cache:controller",
        "--set",
        "controller.cache-to=type=registry,ref=registry.example/seasonalweather-cache:controller,mode=max",
        "controller",
    ]


def test_swwp_registration_defaults_to_current_build_identity() -> None:
    fields = Register.model_fields

    assert fields["software_version"].default_factory is not None
    assert fields["build_identity"].default_factory is not None


def test_version_endpoint_returns_the_supplied_immutable_build_info() -> None:
    info = _info()
    app = create_app(cast(OrchestratorControl, object()), build_info=info)

    async def principal() -> ApiPrincipal:
        return ApiPrincipal(subject="test", scopes=frozenset({"read:status"}), client_host="test")

    app.dependency_overrides[get_api_principal] = principal

    async def request() -> httpx2.Response:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/v1/version")

    response = asyncio.run(request())

    assert response.status_code == 200
    assert response.json() == info.to_dict()
    assert response.headers["cache-control"] == "no-store"


def test_image_matrix_declares_all_revision_profiles() -> None:
    matrix = (ROOT / "docker-bake.hcl").read_text(encoding="utf-8")

    assert all(f'target "{target}"' in matrix for target in IMAGE_TARGETS)
    assert 'target "common"' in matrix
    assert 'variable "SW_VERSION" { default = "0.18.0" }' in matrix
    assert 'variable "SW_BUILD_IDENTITY" { default = "seasonalweather-0.18.0" }' in matrix
    assert '"io.seasonalweather.build.identity"' in matrix
