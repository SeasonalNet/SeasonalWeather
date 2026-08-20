from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import httpx

from seasonalweather.api.api import create_app
from seasonalweather.api.auth import ApiPrincipal, get_api_principal
from seasonalweather.build_metadata import BuildInfo, collect_build_info
from seasonalweather.control import OrchestratorControl
from seasonalweather.swwp.messages import Register
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
    assert first.dirty_tree is True
    assert first.build_identity.endswith("-dirty")
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
    assert labels["org.opencontainers.image.version"] == "0.17.0"
    assert labels["io.seasonalweather.build.identity"] == info.build_identity
    assert labels["io.seasonalweather.build.dirty"] == "true"
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


def test_bake_environment_contains_only_controlled_identity_inputs() -> None:
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

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
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
