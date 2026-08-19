from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from seasonalweather.api.api import create_app
from seasonalweather.api.auth import ApiPrincipal, get_api_principal
from seasonalweather.config import load_config
from seasonalweather.configuration.inspection import effective_configuration, validate_configuration
from seasonalweather.database import SeasonalDatabase
from seasonalweather.runtime_diagnostics.models import (
    CorrelationContext,
    DiagnosticRole,
    PromotionReason,
)
from seasonalweather.runtime_diagnostics.repository import OccurrenceRepository
from seasonalweather.runtime_diagnostics.service import RuntimeDiagnosticService


def _request(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def _principal() -> ApiPrincipal:
    return ApiPrincipal(
        subject="operator",
        scopes=frozenset({"*"}),
        client_host="127.0.0.1",
    )


class Control:
    def __init__(self) -> None:
        self.uploads = 0

    def audio_upload_max_bytes(self) -> int:
        return 64

    async def stage_wav_upload(self, **_kwargs: Any) -> dict[str, Any]:
        self.uploads += 1
        return {
            "asset_id": "aud_00000000000000000001",
            "filename": "alert.wav",
            "content_type": "audio/wav",
            "duration_seconds": 1.0,
            "sample_rate_hz": 48_000,
            "target_sample_rate_hz": 48_000,
            "channels": 2,
            "sample_width_bytes": 2,
            "frames": 48_000,
            "normalized": True,
            "sha256": "a" * 64,
            "uploaded_at": "2026-08-19T00:00:00+00:00",
            "expires_at": "2026-08-20T00:00:00+00:00",
        }

    async def get_config_schema(self) -> dict[str, object]:
        return {"config_schema": 1, "schema": {"type": "object"}}

    async def get_effective_config(self) -> dict[str, object]:
        return {"config_schema": 1, "configuration": {"safe": True}, "redacted": True}

    async def validate_config(self, **_kwargs: Any) -> dict[str, object]:
        return {"valid": True, "preflight_ready": True, "report": {"valid": True}, "redacted": True}


class ValidationControl(Control):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path

    async def validate_config(self, **kwargs: Any) -> dict[str, object]:
        return await validate_configuration(
            str(self.config_path),
            preflight=bool(kwargs.get("preflight", False)),
            warnings_as_errors=bool(kwargs.get("warnings_as_errors", False)),
        )


def _app(control: Any | None = None, *, diagnostic_service: RuntimeDiagnosticService | None = None) -> Any:
    app = create_app(control or Control(), diagnostic_service=diagnostic_service)

    async def principal() -> ApiPrincipal:
        return _principal()

    app.dependency_overrides[get_api_principal] = principal
    return app


def test_sync_command_response_includes_terminal_result() -> None:
    control = SimpleNamespace(
        set_heightened_mode=lambda **_kwargs: _async_result({"mode": "heightened"}),
    )
    response = _request(
        _app(control),
        "POST",
        "/v1/mode/heightened",
        json={"minutes": 5, "reason": "operator test"},
        headers={"Idempotency-Key": "sync-command-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["finished_at"]
    assert body["result"]["details"]["mode"] == "heightened"


def test_upload_idempotency_reuses_one_staged_asset_and_conflicts_on_changed_bytes() -> None:
    control = Control()
    app = _app(control)
    headers = {"Idempotency-Key": "upload-command-1"}
    first = _request(
        app,
        "POST",
        "/v1/uploads/audio",
        files={"file": ("alert.wav", b"bounded-audio", "audio/wav")},
        headers=headers,
    )
    replay = _request(
        app,
        "POST",
        "/v1/uploads/audio",
        files={"file": ("alert.wav", b"bounded-audio", "audio/wav")},
        headers=headers,
    )
    conflict = _request(
        app,
        "POST",
        "/v1/uploads/audio",
        files={"file": ("alert.wav", b"different-audio", "audio/wav")},
        headers=headers,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["asset_id"] == replay.json()["asset_id"]
    assert replay.json()["idempotent_replay"] is True
    assert control.uploads == 1
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


def test_upload_size_is_rejected_before_control_mutation() -> None:
    control = Control()
    response = _request(
        _app(control),
        "POST",
        "/v1/uploads/audio",
        files={"file": ("alert.wav", b"x" * 65, "audio/wav")},
        headers={"Idempotency-Key": "upload-too-large"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "upload_too_large"
    assert control.uploads == 0


def test_configuration_surfaces_are_declared_and_safe() -> None:
    app = _app()
    assert _request(app, "GET", "/v1/config/schema").status_code == 200
    effective = _request(app, "GET", "/v1/config/effective")
    validation = _request(app, "POST", "/v1/config/validate", json={})

    assert effective.status_code == 200
    assert effective.json()["redacted"] is True
    assert validation.status_code == 200
    assert validation.json()["valid"] is True


def test_config_validation_returns_structured_source_location(tmp_path: Path) -> None:
    candidate = tmp_path / "invalid.yaml"
    candidate.write_text("config_schema: 1\nstation: [\n", encoding="utf-8")

    response = _request(
        _app(ValidationControl(candidate)),
        "POST",
        "/v1/config/validate",
        json={},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    issues = [issue for stage in body["report"]["stages"] for issue in stage["issues"]]
    issue = next(issue for issue in issues if "primary_location" in issue)
    location = issue["primary_location"]
    assert location["source"] == "<configured-source>"
    assert location["span"]["start"]["line"] >= 0
    assert location["span"]["start"]["column"] >= 0


def test_diagnostic_catalog_and_occurrence_surfaces_are_bounded(tmp_path: Path) -> None:
    database = SeasonalDatabase(path=str(tmp_path / "diagnostics.sqlite3"))
    database.bootstrap()
    service = RuntimeDiagnosticService(OccurrenceRepository(database))
    service.initialize()
    service.promote(
        service.build(
            code="SWRUN4001",
            context=CorrelationContext(
                role=DiagnosticRole.CONTROLLER,
                instance_id="controller_00000001",
                component="api-test",
                reason_code="optional_task_failed",
            ),
            message="bounded diagnostic",
            operational_effect="the optional component is degraded",
            recovery_action="inspect the component",
            promotion_reason=PromotionReason.DEGRADATION,
        )
    )

    app = _app(diagnostic_service=service)
    catalog = _request(app, "GET", "/v1/diagnostics/catalog")
    detail = _request(app, "GET", "/v1/diagnostics/catalog/SWRUN4001")
    active = _request(app, "GET", "/v1/diagnostics/active?limit=1")
    history = _request(app, "GET", "/v1/diagnostics/history?limit=1")

    assert catalog.status_code == 200
    assert detail.status_code == 200
    assert detail.json()["diagnostic"]["code"] == "SWRUN4001"
    assert active.status_code == 200
    assert len(active.json()["occurrences"]) == 1
    assert history.status_code == 200
    assert len(history.json()["occurrences"]) == 1


def test_effective_projection_does_not_expose_secret_or_local_path(monkeypatch: Any) -> None:
    monkeypatch.setenv("SEASONAL_API_TOKEN", "P1-22-SECRET-SENTINEL")
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "P1-22-PASSWORD-SENTINEL")
    config_path = Path("config/config.yaml")
    config = load_config(str(config_path))
    rendered = repr(effective_configuration(config))

    assert "P1-22-SECRET-SENTINEL" not in rendered
    assert "P1-22-PASSWORD-SENTINEL" not in rendered
    assert str(config_path.resolve()) not in rendered


async def _async_result(value: dict[str, Any]) -> dict[str, Any]:
    return value
