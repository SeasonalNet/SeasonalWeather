from __future__ import annotations

import asyncio
from typing import Any

import httpx2

from seasonalweather.api.api import create_app
from seasonalweather.api.auth import ApiPrincipal, get_api_principal


class FakeControl:
    async def get_public_handled_alerts(self) -> dict[str, Any]:
        return {
            "stationId": "seasonalweather",
            "generatedAt": "2026-05-30T00:00:00+00:00",
            "source": "seasonalweather",
            "alerts": [],
        }

    async def get_status(self) -> dict[str, Any]:
        return {}


def _request(
    app: Any,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
) -> httpx2.Response:
    async def send() -> httpx2.Response:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, json=json)

    return asyncio.run(send())


def test_openapi_is_31_and_documents_problem_details() -> None:
    res = _request(create_app(FakeControl()), "GET", "/openapi.json")

    assert res.status_code == 200
    spec = res.json()
    assert spec["openapi"] == "3.1.0"
    assert spec["jsonSchemaDialect"] == "https://json-schema.org/draft/2020-12/schema"
    assert "ProblemDetails" in spec["components"]["schemas"]
    assert (
        spec["paths"]["/v1/status"]["get"]["responses"]["403"]["content"]["application/problem+json"]["schema"]["$ref"]
        == "#/components/schemas/ProblemDetails"
    )


def test_public_handled_alerts_still_returns_feed_json() -> None:
    res = _request(create_app(FakeControl()), "GET", "/v1/handled-alerts")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == {
        "stationId": "seasonalweather",
        "generatedAt": "2026-05-30T00:00:00+00:00",
        "source": "seasonalweather",
        "alerts": [],
    }


def test_api_errors_are_problem_details() -> None:
    app = create_app(FakeControl())

    async def fake_principal() -> ApiPrincipal:
        return ApiPrincipal(subject="test", scopes=frozenset({"*"}), client_host="testclient")

    app.dependency_overrides[get_api_principal] = fake_principal
    res = _request(app, "POST", "/v1/cycle/rebuild", json={"reason": "test"})

    assert res.status_code == 400
    assert res.headers["content-type"].startswith("application/problem+json")
    body = res.json()
    assert body["type"] == "/problems/missing-idempotency-key"
    assert body["status"] == 400
    assert body["code"] == "missing_idempotency_key"
    assert body["detail"]
    assert body["request_id"].startswith("req_")
    assert res.headers["x-request-id"] == body["request_id"]
