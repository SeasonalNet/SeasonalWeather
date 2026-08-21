from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
import yaml

from seasonalweather.api.api import create_app
from seasonalweather.auth import AuthenticationError, AuthenticationRepository, AuthenticationService
from seasonalweather.auth.credentials import (
    parse_access_token,
    parse_client_credential,
)
from seasonalweather.auth.service import route_is_allowed
from seasonalweather.config import load_config
from seasonalweather.database.core import SeasonalDatabase

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeControl:
    async def get_status(self) -> dict[str, Any]:
        return {"ok": True}


def _policy() -> SimpleNamespace:
    return SimpleNamespace(
        minimum_ttl_seconds=60,
        default_ttl_seconds=900,
        maximum_read_ttl_seconds=3600,
        maximum_write_ttl_seconds=900,
    )


def _service(tmp_path: Path, *, clock: Any = None) -> AuthenticationService:
    repository = AuthenticationRepository(SeasonalDatabase(path=str(tmp_path / "auth.sqlite3")))
    return AuthenticationService(repository, _policy(), **({"clock": clock} if clock else {}))


def _client(service: AuthenticationService, **overrides: Any):
    values = {
        "subject": "automation",
        "scopes": ["read:status"],
        "route_prefixes": ["/v1/status"],
        "cidrs": ["127.0.0.1/32", "::1/128"],
    }
    values.update(overrides)
    return service.create_client(**values)


def _request(app: Any, method: str, path: str, **kwargs: Any) -> httpx2.Response:
    async def send() -> httpx2.Response:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def _exchange_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str = "exchange"):
    raw = yaml.safe_load((REPO_ROOT / "config/config.yaml").read_text(encoding="utf-8"))
    raw["api"]["auth"]["mode"] = mode
    raw["api"]["allow_remote"] = True
    raw["database"]["path"] = str(tmp_path / "auth.sqlite3")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "synthetic-source-password")
    return load_config(str(path))


def test_credentials_use_disjoint_strict_formats_and_storage_is_one_way(tmp_path: Path) -> None:
    service = _service(tmp_path)
    issued = _client(service)
    token = service.issue_token(client_credential=issued.credential, source_ip="127.0.0.1")

    assert parse_client_credential(issued.credential) is not None
    assert parse_access_token(issued.credential) is None
    assert parse_access_token(token.access_token) is not None
    assert parse_client_credential(token.access_token) is None
    for malformed in (
        issued.credential.upper(),
        issued.credential[:-1],
        issued.credential + "x",
        "swc_missing",
        "swa_missing",
    ):
        assert parse_client_credential(malformed) is None
        assert parse_access_token(malformed) is None

    stored = (tmp_path / "auth.sqlite3").read_bytes()
    assert issued.credential.encode() not in stored
    assert token.access_token.encode() not in stored
    assert issued.credential not in repr(issued.client)


def test_repository_recreation_and_client_lifecycle_invalidate_tokens(tmp_path: Path) -> None:
    service = _service(tmp_path)
    issued = _client(service)
    token = service.issue_token(client_credential=issued.credential, source_ip="127.0.0.1")

    recreated = _service(tmp_path)
    assert recreated.show_client(issued.client.client_id).subject == "automation"
    replacement = recreated.rotate_client(issued.client.client_id)
    assert replacement.client.generation == 2
    with pytest.raises(AuthenticationError, match="Client authentication failed"):
        recreated.issue_token(client_credential=issued.credential, source_ip="127.0.0.1")
    with pytest.raises(AuthenticationError, match="Bearer token was not recognized"):
        recreated.authorize_access(token.access_token, source_ip="127.0.0.1", path="/v1/status")

    second = recreated.issue_token(client_credential=replacement.credential, source_ip="127.0.0.1")
    recreated.disable_client(issued.client.client_id)
    with pytest.raises(AuthenticationError):
        recreated.authorize_access(second.access_token, source_ip="127.0.0.1", path="/v1/status")
    recreated.enable_client(issued.client.client_id)
    with pytest.raises(AuthenticationError):
        recreated.authorize_access(second.access_token, source_ip="127.0.0.1", path="/v1/status")
    recreated.revoke_client(issued.client.client_id)
    assert recreated.revoke_client(issued.client.client_id).revoked_at is not None
    with pytest.raises(AuthenticationError):
        recreated.enable_client(issued.client.client_id)


def test_scope_ttl_route_cidr_and_expiration_policy(tmp_path: Path) -> None:
    service = _service(tmp_path)
    issued = _client(
        service,
        scopes=["read:status", "control:cycle"],
        route_prefixes=["/v1"],
    )
    read = service.issue_token(
        client_credential=issued.credential,
        source_ip="127.0.0.1",
        requested_scopes=["read:status"],
        requested_ttl=3600,
    )
    assert read.expires_in == 3600
    with pytest.raises(AuthenticationError, match="TTL"):
        service.issue_token(
            client_credential=issued.credential,
            source_ip="127.0.0.1",
            requested_scopes=["control:cycle"],
            requested_ttl=901,
        )
    with pytest.raises(AuthenticationError, match="exceed"):
        service.issue_token(
            client_credential=issued.credential,
            source_ip="127.0.0.1",
            requested_scopes=["read:health"],
        )
    with pytest.raises(AuthenticationError, match="policy"):
        service.issue_token(client_credential=issued.credential, source_ip="192.0.2.1")

    assert route_is_allowed("/v1/segments/current", ("/v1/segments",), False)
    assert not route_is_allowed("/v1/segments-extra", ("/v1/segments",), False)
    assert route_is_allowed("/anything", (), True)


def test_revocation_is_owned_idempotent_non_enumerating_and_audited(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = _client(service)
    second = _client(service, subject="other")
    token = service.issue_token(client_credential=first.credential, source_ip="127.0.0.1")

    service.revoke_token(
        client_credential=second.credential,
        target_token=token.access_token,
        source_ip="127.0.0.1",
    )
    service.authorize_access(token.access_token, source_ip="127.0.0.1", path="/v1/status")
    service.revoke_token(
        client_credential=first.credential,
        target_token=token.access_token,
        source_ip="127.0.0.1",
    )
    service.revoke_token(
        client_credential=first.credential,
        target_token=token.access_token,
        source_ip="127.0.0.1",
    )
    with pytest.raises(AuthenticationError):
        service.authorize_access(token.access_token, source_ip="127.0.0.1", path="/v1/status")

    events = service.repository.list_audit_events()
    assert [event["event_type"] for event in events].count("token.revoked") == 1
    assert all("swc_" not in repr(event) and "swa_" not in repr(event) for event in events)


def test_last_used_is_coalesced_to_one_write_per_minute(tmp_path: Path) -> None:
    now = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
    current = [now]
    service = _service(tmp_path, clock=lambda: current[0])
    issued = _client(service)
    token = service.issue_token(client_credential=issued.credential, source_ip="127.0.0.1")
    writes = 0
    persist = service.repository.coalesce_last_used

    def counted_persist(**kwargs: Any) -> None:
        nonlocal writes
        writes += 1
        persist(**kwargs)

    service.repository.coalesce_last_used = counted_persist  # type: ignore[method-assign]
    service.authorize_access(token.access_token, source_ip="127.0.0.1", path="/v1/status")
    first = service.repository.get_token(parse_access_token(token.access_token).public_id)
    current[0] += dt.timedelta(seconds=30)
    service.authorize_access(token.access_token, source_ip="127.0.0.1", path="/v1/status")
    assert service.repository.get_token(first.token_id).last_used_at == first.last_used_at
    assert writes == 1
    current[0] += dt.timedelta(seconds=31)
    service.authorize_access(token.access_token, source_ip="127.0.0.1", path="/v1/status")
    assert service.repository.get_token(first.token_id).last_used_at == current[0]
    assert writes == 2


def test_exchange_and_protected_route_api_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from seasonalweather import main

    cfg = _exchange_config(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_APP_CFG", cfg)
    service = _service(tmp_path)
    issued = _client(service)
    app = create_app(FakeControl(), auth_service=service)
    client_header = {"Authorization": f"SeasonalClient {issued.credential}"}

    response = _request(app, "POST", "/v1/auth/token", headers=client_header, json={})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    token = response.json()["access_token"]
    protected = _request(
        app,
        "GET",
        "/v1/status",
        headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "192.0.2.1"},
    )
    assert protected.status_code == 200

    revoked = _request(
        app,
        "POST",
        "/v1/auth/revoke",
        headers=client_header,
        json={"token": token},
    )
    assert revoked.status_code == 200
    assert token not in revoked.text
    assert _request(app, "GET", "/v1/status", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_hybrid_keeps_static_and_access_paths_separate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from seasonalweather import main

    static_token = "synthetic-hybrid-static-token"
    monkeypatch.setenv("SEASONAL_API_TOKEN", static_token)
    cfg = _exchange_config(monkeypatch, tmp_path, mode="hybrid")
    monkeypatch.setattr(main, "_APP_CFG", cfg)
    service = _service(tmp_path)
    issued = _client(service)
    access = service.issue_token(client_credential=issued.credential, source_ip="127.0.0.1")
    app = create_app(FakeControl(), auth_service=service)

    assert _request(app, "GET", "/v1/status", headers={"Authorization": f"Bearer {static_token}"}).status_code == 200
    assert (
        _request(
            app,
            "GET",
            "/v1/status",
            headers={"Authorization": f"Bearer {access.access_token}"},
        ).status_code
        == 200
    )
    assert (
        _request(app, "GET", "/v1/status", headers={"Authorization": f"Bearer {issued.credential}"}).status_code == 401
    )


def test_malformed_revocation_target_is_never_echoed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from seasonalweather import main

    cfg = _exchange_config(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_APP_CFG", cfg)
    service = _service(tmp_path)
    issued = _client(service)
    sentinel = "SENTINEL-MALFORMED-REVOCATION-TARGET"
    response = _request(
        create_app(FakeControl(), auth_service=service),
        "POST",
        "/v1/auth/revoke",
        headers={"Authorization": f"SeasonalClient {issued.credential}"},
        json={"token": sentinel},
    )

    assert response.status_code == 400
    assert sentinel not in response.text
    assert sentinel not in caplog.text


def test_static_mode_rejects_exchange_and_openapi_distinguishes_schemes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from seasonalweather import main

    cfg = _exchange_config(monkeypatch, tmp_path, mode="static")
    monkeypatch.setattr(main, "_APP_CFG", cfg)
    app = create_app(FakeControl())
    denied = _request(app, "POST", "/v1/auth/token", json={})
    assert denied.status_code == 403
    spec = _request(app, "GET", "/openapi.json").json()
    assert spec["paths"]["/v1/auth/token"]["post"]["security"] == [{"SeasonalClientAuth": []}]
    assert spec["paths"]["/v1/status"]["get"]["security"] == [{"BearerAuth": []}]


def test_schema_migration_is_repeatable_and_contains_no_raw_columns(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _client(service)
    _service(tmp_path)
    with sqlite3.connect(tmp_path / "auth.sqlite3") as conn:
        columns = {
            row[1]
            for table in ("auth_clients", "auth_access_tokens")
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
    assert not {"credential", "secret", "access_token", "raw_token"}.intersection(columns)
