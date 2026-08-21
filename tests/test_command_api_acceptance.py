from __future__ import annotations

import asyncio
from typing import Any

import httpx2

from seasonalweather.api.api import create_app
from seasonalweather.api.auth import ApiPrincipal, get_api_principal


class FakeControl:
    def __init__(self) -> None:
        self.rebuilds = 0

    async def rebuild_cycle(self, *, reason: str | None, actor: str) -> dict[str, Any]:
        self.rebuilds += 1
        return {"ok": True, "reason": reason, "actor": actor}


def _request(app: Any, method: str, path: str, **kwargs: Any) -> httpx2.Response:
    async def send() -> httpx2.Response:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def _app() -> Any:
    app = create_app(FakeControl())

    async def principal() -> ApiPrincipal:
        return ApiPrincipal(subject="operator", scopes=frozenset({"*"}), client_host="127.0.0.1")

    app.dependency_overrides[get_api_principal] = principal
    return app


def test_cycle_rebuild_returns_truthful_202_accepted_command() -> None:
    app = _app()
    response = _request(
        app,
        "POST",
        "/v1/cycle/rebuild",
        json={"reason": "refresh"},
        headers={"Idempotency-Key": "cycle-refresh-1"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["command_type"] == "cycle.rebuild"
    assert body["status_url"] == f"/v1/commands/{body['command_id']}"
    assert "result" not in body
    assert "refresh" not in str(body)

    snapshot = _request(app, "GET", body["status_url"])
    assert snapshot.status_code == 200
    assert snapshot.json()["status"] == "accepted"
    assert snapshot.json()["result"] is None


def test_openapi_documents_cycle_rebuild_202_and_bounded_command_schema() -> None:
    spec = _request(_app(), "GET", "/openapi.json").json()
    operation = spec["paths"]["/v1/cycle/rebuild"]["post"]
    assert "202" in operation["responses"]
    accepted = spec["components"]["schemas"]["CommandAccepted"]
    assert "status_url" in accepted["properties"]
