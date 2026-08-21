from __future__ import annotations

import asyncio
import datetime as dt
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
import yaml

from seasonalweather.api.api import create_app
from seasonalweather.api.commands import CommandStore
from seasonalweather.auth import AuthenticationRepository, AuthenticationService
from seasonalweather.capabilities.registry import CapabilityRegistry
from seasonalweather.config import load_config
from seasonalweather.database.core import SeasonalDatabase
from seasonalweather.health_service import (
    ComponentProbe,
    ComponentState,
    HealthComponent,
    HealthService,
    build_runtime_health_service,
)
from seasonalweather.jobs.policies import JobType
from seasonalweather.lifecycle import Lifecycle
from seasonalweather.swwp.capability_adapter import manifest_from_wire
from tests.support.capabilities import wire_manifest, wire_record

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeControl:
    async def get_status(self) -> dict[str, Any]:
        return {"ok": True}


def _request(
    app: Any,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    async def send() -> httpx2.Response:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, headers=headers)

    return asyncio.run(send())


def _component_probe(
    name: str,
    state: ComponentState,
    *,
    required: bool,
    reason: str,
) -> ComponentProbe:
    async def evaluate() -> HealthComponent:
        return HealthComponent(name, state, required, reason)

    return ComponentProbe(name, required, evaluate)


def _healthy_service() -> HealthService:
    return HealthService(
        [
            _component_probe(
                "conductor",
                ComponentState.HEALTHY,
                required=True,
                reason="conductor_running",
            ),
            _component_probe(
                "source_nwws_oi",
                ComponentState.DEGRADED,
                required=False,
                reason="source_stale",
            ),
        ]
    )


def _static_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    token: str,
    mode: str = "static",
) -> Any:
    raw = yaml.safe_load((REPO_ROOT / "config/config.yaml").read_text(encoding="utf-8"))
    raw["api"]["allow_remote"] = True
    raw["api"]["auth"]["mode"] = mode
    raw["api"]["auth"]["scopes"] = "read:health"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "ICECAST_SOURCE_PASSWORD",
        "synthetic-source-password",
    )
    monkeypatch.setenv("SEASONAL_API_TOKEN", token)
    return load_config(str(config_path))


def _auth_service(tmp_path: Path) -> AuthenticationService:
    database = SeasonalDatabase(path=str(tmp_path / "auth.sqlite3"))
    policy = SimpleNamespace(
        minimum_ttl_seconds=60,
        default_ttl_seconds=900,
        maximum_read_ttl_seconds=3600,
        maximum_write_ttl_seconds=900,
    )
    return AuthenticationService(
        AuthenticationRepository(database),
        policy,
    )


def test_healthz_is_public_minimal_and_does_not_collect_health() -> None:
    class ExplodingHealthService:
        async def collect(self) -> None:
            raise AssertionError("health collection must not run")

    app = create_app(
        FakeControl(),
        health_service=ExplodingHealthService(),  # type: ignore[arg-type]
    )

    response = _request(
        app,
        "GET",
        "/healthz",
        headers={"Authorization": "Bearer SENTINEL-LIVENESS-TOKEN"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert "SENTINEL" not in response.text


@pytest.mark.parametrize("mode", ["static", "exchange", "hybrid"])
def test_public_health_routes_require_no_principal_in_any_auth_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    from seasonalweather import main

    raw = yaml.safe_load((REPO_ROOT / "config/config.yaml").read_text(encoding="utf-8"))
    raw["api"]["allow_remote"] = True
    raw["api"]["auth"]["mode"] = mode
    raw["database"]["path"] = str(tmp_path / f"{mode}.sqlite3")
    config_path = tmp_path / f"{mode}.yaml"
    config_path.write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "ICECAST_SOURCE_PASSWORD",
        "synthetic-source-password",
    )
    if mode in {"static", "hybrid"}:
        monkeypatch.setenv(
            "SEASONAL_API_TOKEN",
            f"synthetic-{mode}-health-token",
        )
    else:
        monkeypatch.delenv("SEASONAL_API_TOKEN", raising=False)
    monkeypatch.setattr(main, "_APP_CFG", load_config(str(config_path)))
    app = create_app(FakeControl(), health_service=_healthy_service())

    assert _request(app, "GET", "/healthz").status_code == 200
    assert _request(app, "GET", "/readyz").status_code == 200


def test_readyz_is_public_and_optional_degradation_remains_ready() -> None:
    response = _request(
        create_app(FakeControl(), health_service=_healthy_service()),
        "GET",
        "/readyz",
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["state"] == "degraded"
    assert response.json()["ready"] is True
    assert response.json()["components"][1] == {
        "name": "source_nwws_oi",
        "state": "degraded",
        "required": False,
        "reason": "source_stale",
    }


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("exception", "probe_exception"),
        ("timeout", "probe_timeout"),
    ],
)
def test_required_probe_failure_is_bounded_unready(
    failure: str,
    reason: str,
) -> None:
    sentinel = "SENTINEL-RAW-PROBE-EXCEPTION"

    async def evaluate() -> HealthComponent:
        if failure == "exception":
            raise RuntimeError(sentinel)
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    service = HealthService(
        [ComponentProbe("liquidsoap", True, evaluate)],
        timeout_seconds=0.02,
    )
    started = time.monotonic()
    response = _request(
        create_app(FakeControl(), health_service=service),
        "GET",
        "/readyz",
    )

    assert time.monotonic() - started < 0.5
    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert response.json()["components"][0]["reason"] == reason
    assert sentinel not in response.text


def test_readiness_aggregation_is_deterministic() -> None:
    service = HealthService(
        [
            _component_probe(
                "z_optional",
                ComponentState.NOT_APPLICABLE,
                required=False,
                reason="not_implemented",
            ),
            _component_probe(
                "a_required",
                ComponentState.HEALTHY,
                required=True,
                reason="available",
            ),
            _component_probe(
                "m_disabled",
                ComponentState.DISABLED,
                required=False,
                reason="disabled_by_configuration",
            ),
        ],
        clock=lambda: dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
    )

    first = asyncio.run(service.collect()).to_dict(detailed=True)
    second = asyncio.run(service.collect()).to_dict(detailed=True)

    assert first["ready"] is True
    assert first["state"] == "healthy"
    assert [item["name"] for item in first["components"]] == [
        "a_required",
        "m_disabled",
        "z_optional",
    ]
    assert first["checked_at"] == second["checked_at"]


@pytest.mark.parametrize("mode", ["static", "hybrid"])
def test_v1_health_requires_read_health_static_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    from seasonalweather import main

    token = "synthetic-health-static-token"
    monkeypatch.setattr(
        main,
        "_APP_CFG",
        _static_config(monkeypatch, tmp_path, token=token, mode=mode),
    )
    app = create_app(FakeControl(), health_service=_healthy_service())

    assert _request(app, "GET", "/v1/health").status_code == 401
    response = _request(
        app,
        "GET",
        "/v1/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_sqlite_probe_does_not_create_or_bootstrap_missing_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.sqlite3"
    database = SeasonalDatabase(path=str(path))

    assert database.is_operational() is False
    assert path.exists() is False

    database.bootstrap()
    assert database.is_operational() is True


def test_access_token_principal_can_read_detailed_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from seasonalweather import main

    raw = yaml.safe_load((REPO_ROOT / "config/config.yaml").read_text(encoding="utf-8"))
    raw["api"]["allow_remote"] = True
    raw["api"]["auth"]["mode"] = "exchange"
    raw["database"]["path"] = str(tmp_path / "auth.sqlite3")
    config_path = tmp_path / "exchange.yaml"
    config_path.write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "ICECAST_SOURCE_PASSWORD",
        "synthetic-source-password",
    )
    monkeypatch.setattr(main, "_APP_CFG", load_config(str(config_path)))
    auth_service = _auth_service(tmp_path)
    issued = auth_service.create_client(
        subject="health-reader",
        scopes=["read:health"],
        route_prefixes=["/v1/health"],
        cidrs=["127.0.0.1/32"],
    )
    access = auth_service.issue_token(
        client_credential=issued.credential,
        source_ip="127.0.0.1",
    )
    app = create_app(
        FakeControl(),
        auth_service=auth_service,
        health_service=_healthy_service(),
    )

    response = _request(
        app,
        "GET",
        "/v1/health",
        headers={"Authorization": f"Bearer {access.access_token}"},
    )

    assert response.status_code == 200
    assert access.access_token not in response.text


def test_openapi_health_security_matches_route_policy() -> None:
    spec = _request(
        create_app(FakeControl(), health_service=_healthy_service()),
        "GET",
        "/openapi.json",
    ).json()

    assert spec["paths"]["/healthz"]["get"]["security"] == []
    assert spec["paths"]["/readyz"]["get"]["security"] == []
    assert spec["paths"]["/v1/health"]["get"]["security"] == [{"BearerAuth": []}]
    assert (
        spec["paths"]["/readyz"]["get"]["responses"]["503"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/Readiness"
    )


def test_required_worker_capabilities_are_bounded_without_silent_truncation(
    tmp_path: Path,
) -> None:
    lifecycle = Lifecycle()
    lifecycle.mark_running()
    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            database=SimpleNamespace(enabled=False),
            jobs=SimpleNamespace(enabled=False, required=False),
            api=SimpleNamespace(auth=SimpleNamespace(mode=SimpleNamespace(value="static"))),
            ipaws=SimpleNamespace(enabled=False),
            ern=SimpleNamespace(enabled=False),
        ),
        database=None,
        telnet=SimpleNamespace(ping=lambda **_kwargs: True),
        tts=SimpleNamespace(availability=lambda: (True, "tts_available")),
        health_state=SimpleNamespace(source_snapshot=lambda _source: None),
        lifecycle=lifecycle,
        _seg_store=SimpleNamespace(
            health_snapshot=lambda: {
                "count": 1,
                "ready_count": 1,
                "stale_count": 0,
                "placeholder_count": 0,
                "oldest_age_seconds": 0,
            }
        ),
        _paths=lambda: (tmp_path,),
    )
    capability_registry = SimpleNamespace(
        health=lambda _now: SimpleNamespace(
            active_assignments=0,
            connected_workers=0,
            effective_capacity=0,
            outstanding_probes=0,
            qualified_workers=0,
            stale_capabilities=0,
            unknown_workers=0,
        ),
        snapshots=lambda _now: (),
    )
    required = tuple(f"capability.test.{index:02d}" for index in range(17))
    service = build_runtime_health_service(
        runtime,
        command_store=CommandStore(),
        auth_service=None,
        capability_registry=capability_registry,
        required_capabilities=required,
    )

    report = asyncio.run(service.collect())
    workers = next(component for component in report.components if component.name == "workers")

    assert workers.state is ComponentState.UNAVAILABLE
    assert workers.details["missing_required"] == 17
    with pytest.raises(ValueError, match="required worker capabilities exceed supported maximum"):
        build_runtime_health_service(
            runtime,
            command_store=CommandStore(),
            auth_service=None,
            capability_registry=capability_registry,
            required_capabilities=tuple(f"capability.test.{index:02d}" for index in range(65)),
        )


def test_runtime_report_is_truthful_bounded_and_secret_free(
    tmp_path: Path,
) -> None:
    paths = tuple(tmp_path / name for name in ("work", "audio", "cache", "log"))
    for path in paths:
        path.mkdir()

    class SourceHealth:
        def source_snapshot(self, source: str) -> dict[str, Any]:
            if source == "nwws_oi":
                return {
                    "state": "degraded",
                    "reason": "source_stale",
                    "role": "alert_redundant",
                    "observed_at": dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
                    "age_seconds": 99_999_999,
                    "consecutive_failures": 4,
                    "raw_error": "SENTINEL-SOURCE-PAYLOAD",
                }
            return {
                "state": "healthy",
                "reason": "source_current",
                "role": "forecast",
                "age_seconds": 2,
                "consecutive_failures": 0,
            }

    lifecycle = Lifecycle()
    lifecycle.mark_running()
    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            database=SimpleNamespace(enabled=False),
            jobs=SimpleNamespace(enabled=True, required=True),
            api=SimpleNamespace(auth=SimpleNamespace(mode=SimpleNamespace(value="static"))),
            ipaws=SimpleNamespace(enabled=False),
            ern=SimpleNamespace(enabled=False),
        ),
        database=None,
        telnet=SimpleNamespace(ping=lambda **_kwargs: True),
        tts=SimpleNamespace(availability=lambda: (True, "tts_available")),
        health_state=SourceHealth(),
        lifecycle=lifecycle,
        _seg_store=SimpleNamespace(
            health_snapshot=lambda: {
                "count": 5,
                "ready_count": 4,
                "stale_count": 1,
                "placeholder_count": 0,
                "oldest_age_seconds": 500,
            }
        ),
        _paths=lambda: paths,
    )
    capability_registry = CapabilityRegistry(allowed_capabilities=frozenset({"tts.synthesis.v1"}))
    capability_registry.register(
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        manifest=manifest_from_wire(
            wire_manifest(
                (
                    wire_record(
                        "tts.synthesis.v1",
                        now=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
                        parameters={"format": "wav"},
                    ),
                )
            )
        ),
        authorized_capabilities=frozenset({"tts.synthesis.v1"}),
        authorized_job_types=frozenset({JobType.TTS_SYNTHESIZE}),
        payload_versions={JobType.TTS_SYNTHESIZE: 1},
        result_versions={JobType.TTS_SYNTHESIZE: 1},
        now=dt.datetime.now(dt.UTC),
    )
    service = build_runtime_health_service(
        runtime,
        command_store=CommandStore(),
        auth_service=None,
        job_service=SimpleNamespace(
            health=lambda: SimpleNamespace(
                initialized=True,
                wal=True,
                schema_version=1,
                reconciliation_required=0,
                details=lambda: {
                    "initialized": True,
                    "schema_version": 1,
                    "wal": True,
                    "admission_open": True,
                    "active_leases": 0,
                    "overdue_jobs": 0,
                    "cancellation_backlog": 0,
                    "reconciliation_required": 0,
                },
            )
        ),
        capability_registry=capability_registry,
        required_capabilities=("tts.synthesis.v1",),
    )

    async def collect_with_conductor() -> dict[str, Any]:
        async def conductor() -> None:
            await asyncio.sleep(1)

        task = asyncio.create_task(conductor(), name="conductor")
        try:
            return (await service.collect()).to_dict(detailed=True)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    report = asyncio.run(collect_with_conductor())
    components = {item["name"]: item for item in report["components"]}

    assert report["ready"] is True, [
        (item["name"], item["reason"]) for item in report["components"] if item["required"]
    ]
    assert report["state"] == "degraded"
    assert components["sqlite"]["state"] == "disabled"
    assert components["conductor"]["state"] == "healthy"
    assert components["liquidsoap"]["state"] == "healthy"
    assert components["tts"]["state"] == "healthy"
    assert components["segments"]["state"] == "degraded"
    assert components["segments"]["details"]["stale_count"] == 1
    assert components["job_repository"]["state"] == "healthy"
    assert components["job_repository"]["required"] is True
    assert components["workers"]["state"] == "healthy"
    assert components["workers"]["required"] is True
    assert components["workers"]["details"]["qualified_workers"] == 1
    assert components["postgresql"]["state"] == "not_applicable"
    assert components["redis"]["state"] == "not_applicable"
    assert components["source_nwws_oi"]["age_seconds"] == 31_536_000
    assert "SENTINEL" not in repr(report)
    assert str(tmp_path) not in repr(report)
