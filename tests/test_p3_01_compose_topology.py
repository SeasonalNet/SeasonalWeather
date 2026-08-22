from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict[str, Any]:
    value = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_standard_topology_has_required_services_and_explicit_profiles() -> None:
    compose = _compose()
    services = compose["services"]
    assert isinstance(services, dict)

    assert {"controller", "routine-worker", "liquidsoap"}.issubset(services)
    assert services["routine-worker"].get("profiles") is None
    assert services["maintenance-worker"]["profiles"] == ["maintenance"]
    assert services["icecast"]["profiles"] == ["icecast"]


def test_controller_and_workers_use_the_p2_entrypoints_and_internal_swwp() -> None:
    services = _compose()["services"]
    controller = services["controller"]
    routine = services["routine-worker"]
    maintenance = services["maintenance-worker"]

    assert controller["entrypoint"] == ["python", "-m", "seasonalweather.api.server"]
    assert controller["command"][:2] == ["--config", "/etc/seasonalweather/config.yaml"]
    assert controller["healthcheck"]["test"][-3:] == ["controller", "--mode", "readiness"]
    for worker in (routine, maintenance):
        assert worker["entrypoint"] == ["python", "-m", "seasonalweather", "worker"]
        assert "ws://controller:9080/v1/workers/connect" in worker["command"]
        assert worker["depends_on"]["controller"]["condition"] == "service_started"
        assert worker["healthcheck"]["test"][-3:] == ["worker", "--mode", "liveness"]


def test_worker_secret_isolated_and_runtime_hardening_is_declared() -> None:
    services = _compose()["services"]
    controller = services["controller"]
    routine = services["routine-worker"]
    maintenance = services["maintenance-worker"]
    liquidsoap = services["liquidsoap"]

    assert [item["target"] for item in routine["secrets"]] == ["SEASONAL_WORKER_TOKEN"]
    assert [item["target"] for item in maintenance["secrets"]] == ["SEASONAL_WORKER_TOKEN"]
    for worker in (routine, maintenance):
        assert worker["secrets"][0]["uid"] == "10001"
        assert worker["secrets"][0]["gid"] == "10001"
        assert worker["secrets"][0]["mode"] == "0400"
    assert {item["target"] for item in controller["secrets"]} == {
        "ICECAST_SOURCE_PASSWORD",
        "SEASONAL_API_TOKEN",
        "SEASONAL_WORKER_TOKEN",
    }
    assert {item["uid"] for item in controller["secrets"]} == {"10001"}
    assert {item["gid"] for item in controller["secrets"]} == {"10001"}
    assert {item["mode"] for item in controller["secrets"]} == {"0400"}
    assert [item["target"] for item in liquidsoap["secrets"]] == ["ICECAST_SOURCE_PASSWORD"]
    assert liquidsoap["secrets"][0]["uid"] == "1000"
    assert liquidsoap["secrets"][0]["gid"] == "1000"
    assert liquidsoap["secrets"][0]["mode"] == "0400"

    for service in (controller, routine, maintenance, liquidsoap):
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["tmpfs"] == [
            "/tmp:rw,nosuid,nodev,noexec",
            "/run:rw,nosuid,nodev,noexec",
        ]
    for service in (controller, routine, maintenance):
        assert service["user"] == "10001:10001"


def test_p3_01_keeps_remote_tts_and_postgresql_out_of_the_compose_graph() -> None:
    services = _compose()["services"]
    assert not {"seasonal-ttsd", "openai-tts", "postgres", "postgresql"}.intersection(services)
    assert "POSTGRES_HOST" not in str(_compose())
    assert "OPENAI_API_KEY" not in str(_compose())
