from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
import yaml
from fastapi.routing import APIRoute
from starlette.routing import Route

from seasonalweather.api.api import create_app
from seasonalweather.api.auth import ROUTE_AUTH_POLICIES, ApiPrincipal
from seasonalweather.config import load_config
from seasonalweather.control import OrchestratorControl

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeControl:
    async def get_status(self) -> dict[str, Any]:
        return {"ok": True}

    async def get_health(self) -> dict[str, Any]:
        return {"ok": True}

    async def get_public_handled_alerts(self) -> dict[str, Any]:
        return {
            "stationId": "seasonalweather",
            "generatedAt": "2026-07-24T00:00:00+00:00",
            "source": "seasonalweather",
            "alerts": [],
        }


def _request(
    app: Any,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    async def send() -> httpx2.Response:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, headers=headers)

    return asyncio.run(send())


def _load_remote_test_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    token: str,
    scopes: str = "read:status",
):
    raw = yaml.safe_load((REPO_ROOT / "config/config.yaml").read_text(encoding="utf-8"))
    raw["api"]["allow_remote"] = True
    raw["api"]["auth"]["scopes"] = scopes
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "synthetic-source-password")
    monkeypatch.setenv("SEASONAL_API_TOKEN", token)
    return load_config(str(config_path))


def test_existing_static_bearer_authentication_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from seasonalweather import main

    token = "synthetic-static-success-token"
    monkeypatch.setattr(
        main,
        "_APP_CFG",
        _load_remote_test_config(
            monkeypatch,
            tmp_path,
            token=token,
        ),
    )

    response = _request(
        create_app(FakeControl()),
        "GET",
        "/v1/status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Basic synthetic-static-token",
        "Bearer",
        "Bearer ",
        "Bearer incorrect-synthetic-token",
    ],
)
def test_missing_malformed_and_incorrect_bearer_credentials_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authorization: str | None,
) -> None:
    from seasonalweather import main

    monkeypatch.setattr(
        main,
        "_APP_CFG",
        _load_remote_test_config(
            monkeypatch,
            tmp_path,
            token="synthetic-correct-token",
        ),
    )
    headers = {"Authorization": authorization} if authorization else {}

    response = _request(
        create_app(FakeControl()),
        "GET",
        "/v1/status",
        headers=headers,
    )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


def test_scope_authorization_uses_exact_membership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from seasonalweather import main

    token = "synthetic-exact-scope-token"
    monkeypatch.setattr(
        main,
        "_APP_CFG",
        _load_remote_test_config(
            monkeypatch,
            tmp_path,
            token=token,
            scopes="read:status",
        ),
    )
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app(FakeControl())

    assert _request(app, "GET", "/v1/status", headers=headers).status_code == 200
    denied = _request(app, "GET", "/v1/health", headers=headers)
    assert denied.status_code == 403
    assert denied.json()["code"] == "insufficient_scope"

    principal = ApiPrincipal(
        subject="scope-test",
        scopes=frozenset({"read:status-extra"}),
        client_host="testclient",
    )
    assert principal.has_scope("read:status") is False


def test_route_policy_matrix_covers_every_route_and_dependency() -> None:
    app = create_app(FakeControl())
    actual_routes: dict[tuple[str, str], Route] = {}
    for route in app.routes:
        if not isinstance(route, Route):
            continue
        for method in route.methods:
            actual_routes[(method, route.path)] = route

    assert set(actual_routes) == set(ROUTE_AUTH_POLICIES)
    for key, policy in ROUTE_AUTH_POLICIES.items():
        route = actual_routes[key]
        installed = []
        if isinstance(route, APIRoute):
            installed = [
                getattr(dependency.call, "__seasonalweather_auth_policy__", None)
                for dependency in route.dependant.dependencies
                if getattr(
                    dependency.call,
                    "__seasonalweather_auth_policy__",
                    None,
                )
                is not None
            ]
        assert installed == ([] if policy.public else [policy]), key


def test_openapi_route_security_matches_runtime_policy() -> None:
    spec = _request(create_app(FakeControl()), "GET", "/openapi.json").json()

    assert spec["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert spec["paths"]["/v1/handled-alerts"]["get"]["security"] == []
    assert spec["paths"]["/v1/status"]["get"]["security"] == [{"BearerAuth": []}]


def test_effective_configuration_reports_auth_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sentinel = "SENTINEL-EFFECTIVE-CONFIG-TOKEN"
    cfg = _load_remote_test_config(
        monkeypatch,
        tmp_path,
        token=sentinel,
    )
    control = OrchestratorControl(
        SimpleNamespace(cfg=cfg, database=None),
        config_path=str(tmp_path / "config.yaml"),
    )

    summary = asyncio.run(control.get_config_summary())

    assert summary["api"]["auth"]["mode"] == "static"
    assert summary["api"]["auth"]["credential_count"] == 1
    assert summary["api"]["auth"]["legacy_mode_normalized"] is False
    assert summary["api"]["auth"]["legacy_scope_normalized"] is False
    assert summary["api"]["auth"]["exchange_available"] is False
    assert summary["api"]["auth"]["ttl_policy"]["default_seconds"] == 900
    assert sentinel not in repr(summary)


def test_presented_bearer_token_never_appears_in_logs_errors_or_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from seasonalweather import main

    sentinel = "SENTINEL-PRESENTED-BEARER-TOKEN"
    monkeypatch.setattr(
        main,
        "_APP_CFG",
        _load_remote_test_config(
            monkeypatch,
            tmp_path,
            token="synthetic-configured-token",
        ),
    )

    with caplog.at_level(logging.DEBUG):
        response = _request(
            create_app(FakeControl()),
            "GET",
            "/v1/status",
            headers={"Authorization": f"Bearer {sentinel}"},
        )

    assert response.status_code == 401
    assert sentinel not in response.text
    assert sentinel not in caplog.text
