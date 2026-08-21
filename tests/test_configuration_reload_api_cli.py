from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx2

from seasonalweather.api.api import create_app
from seasonalweather.api.auth import ApiPrincipal, get_api_principal
from seasonalweather.cli.config import main as config_main
from seasonalweather.commands.service import CommandStore

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config/config.yaml"


class FakeControl:
    pass


class FakeReloadService:
    def __init__(self, store: CommandStore) -> None:
        self.store = store
        self.requests: list[object] = []

    async def admit(self, request: Any, *, idempotency_key: str):
        self.requests.append(request)
        return await self.store.create_or_replay(
            command_type="config.reload",
            idempotency_key=idempotency_key,
            actor=request.actor,
            reason=request.reason,
            payload=request.command_payload(),
        )


def _request(app: Any, method: str, path: str, **kwargs: Any) -> httpx2.Response:
    async def send() -> httpx2.Response:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def _app() -> tuple[Any, FakeReloadService]:
    store = CommandStore()
    reload_service = FakeReloadService(store)
    app = create_app(FakeControl(), store=store, reload_service=reload_service)

    async def principal() -> ApiPrincipal:
        return ApiPrincipal(subject="operator", scopes=frozenset({"*"}), client_host="127.0.0.1")

    app.dependency_overrides[get_api_principal] = principal
    return app, reload_service


def test_reload_api_returns_durable_202_and_rejects_source_or_secret_input() -> None:
    app, reload_service = _app()
    response = _request(
        app,
        "POST",
        "/v1/config/reload",
        json={"reason": "reviewed change", "dry_run": True, "expected_generation": 4},
        headers={"Idempotency-Key": "reload-api-1"},
    )

    assert response.status_code == 202
    assert response.json()["command_type"] == "config.reload"
    assert reload_service.requests[0].actor == "operator"
    assert reload_service.requests[0].source_path is None

    for prohibited in (
        {"yaml": "secrets: raw-yaml-sensitive-sentinel"},
        {"source_path": "/tmp/private-candidate-sensitive-sentinel"},
        {"api_token": "api-token-sensitive-sentinel"},
    ):
        rejected = _request(
            app,
            "POST",
            "/v1/config/reload",
            json=prohibited,
            headers={
                "Idempotency-Key": f"reject-{next(iter(prohibited))}",
                "X-Request-ID": "req_de868bad6c4048cd",
            },
        )
        assert rejected.status_code == 422
        assert rejected.json()["request_id"] == "req_de868bad6c4048cd"
        assert next(iter(prohibited.values())) not in rejected.text

    cross_principal = _request(
        app,
        "POST",
        "/v1/config/reload",
        json={
            "acknowledgment": {
                "schema_version": 1,
                "actor": "different-principal",
                "candidate_sha256": "a" * 64,
                "candidate_identity_sha256": "b" * 64,
                "report_sha256": "c" * 64,
                "active_generation": 0,
                "warning_identities": ["warning:" + ("d" * 24)],
                "acknowledged_at": dt.datetime(2026, 8, 1, tzinfo=dt.UTC).isoformat(),
                "validator_completed_at": dt.datetime(2026, 8, 1, tzinfo=dt.UTC).isoformat(),
                "expires_at": dt.datetime(2026, 8, 1, 0, 5, tzinfo=dt.UTC).isoformat(),
                "maximum_age_seconds": 300,
                "clock_skew_seconds": 5,
            }
        },
        headers={"Idempotency-Key": "cross-principal-ack"},
    )
    assert cross_principal.status_code == 403
    assert not reload_service.requests[1:]


def test_reload_cli_dry_run_json_is_deterministic_and_redacted(tmp_path: Path, capsys) -> None:
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            "dedupe:\n  ttl_seconds: 900",
            "dedupe:\n  ttl_seconds: 901",
            1,
        ),
        encoding="utf-8",
    )
    arguments = [
        "reload",
        "--dry-run",
        "--config",
        str(EXAMPLE),
        "--candidate",
        str(candidate),
        "--format",
        "json",
    ]

    assert config_main(arguments) == 0
    first = capsys.readouterr().out
    assert config_main(arguments) == 0
    second = capsys.readouterr().out
    assert first == second
    payload = json.loads(first)
    assert payload["outcome"] == "dry_run"
    assert payload["changed_paths"]["live"] == ["/dedupe/ttl_seconds"]
    assert payload["secrets_redacted"] is True
