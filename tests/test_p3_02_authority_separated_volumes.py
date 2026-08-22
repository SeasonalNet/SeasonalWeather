from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from seasonalweather.artifacts.transport import SharedVolumeArtifactTransport

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = "/var/lib/seasonalweather/artifacts"
STAGING_ROOT = f"{ARTIFACT_ROOT}/worker-artifacts/staging"


def _compose() -> dict[str, Any]:
    value = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _mounts(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mounts: dict[str, dict[str, Any]] = {}
    for raw_mount in service.get("volumes", []):
        if isinstance(raw_mount, str):
            source, target, *flags = raw_mount.split(":")
            mounts[target] = {"source": source, "read_only": "ro" in flags}
        else:
            mounts[raw_mount["target"]] = raw_mount
    return mounts


def test_controller_only_mounts_operational_and_job_state() -> None:
    services = _compose()["services"]
    controller = _mounts(services["controller"])
    for service_name in ("routine-worker", "maintenance-worker", "liquidsoap"):
        mounts = _mounts(services[service_name])
        assert "/var/lib/seasonalweather/state" not in mounts
        assert "/var/lib/seasonalweather/jobs" not in mounts
        assert "/var/log/seasonalweather" not in mounts

    assert controller["/var/lib/seasonalweather/state"]["source"] == "seasonalweather-state"
    assert controller["/var/lib/seasonalweather/jobs"]["source"] == "seasonalweather-jobs"


def test_workers_write_only_to_shared_staging_path_and_liquidsoap_is_read_only() -> None:
    services = _compose()["services"]
    controller = _mounts(services["controller"])
    liquidsoap = _mounts(services["liquidsoap"])
    assert controller[ARTIFACT_ROOT]["source"] == "seasonalweather-artifacts"
    assert controller[STAGING_ROOT]["source"] == "seasonalweather-artifact-staging"
    assert not controller[STAGING_ROOT].get("read_only", False)

    for service_name in ("routine-worker", "maintenance-worker"):
        mounts = _mounts(services[service_name])
        assert mounts[ARTIFACT_ROOT]["source"] == "seasonalweather-artifacts"
        assert mounts[ARTIFACT_ROOT]["read_only"] is True
        assert mounts[STAGING_ROOT]["source"] == "seasonalweather-artifact-staging"
        assert not mounts[STAGING_ROOT].get("read_only", False)

    assert liquidsoap[ARTIFACT_ROOT]["source"] == "seasonalweather-artifacts"
    assert liquidsoap[ARTIFACT_ROOT]["read_only"] is True
    assert STAGING_ROOT not in liquidsoap


def test_shared_volume_transport_separates_staging_and_controller_authority(tmp_path: Path) -> None:
    paths = SharedVolumeArtifactTransport(tmp_path / "artifacts").paths
    assert paths.staging == tmp_path / "artifacts" / "worker-artifacts" / "staging"
    assert paths.blobs == tmp_path / "artifacts" / "worker-artifacts" / "blobs"
    assert paths.active == tmp_path / "artifacts" / "worker-artifacts" / "active"
    assert paths.staging != paths.blobs
    assert paths.staging != paths.active
    assert paths.blobs != paths.active


def test_compose_declares_named_persistent_storage_for_required_data() -> None:
    volumes = _compose()["volumes"]
    assert {
        "seasonalweather-state",
        "seasonalweather-jobs",
        "seasonalweather-artifacts",
        "seasonalweather-artifact-staging",
        "seasonalweather-logs",
    }.issubset(volumes)
