from __future__ import annotations

import re
from dataclasses import dataclass

KNOWN_API_SCOPES = frozenset(
    {
        "read:health",
        "read:status",
        "read:alerts",
        "read:config",
        "read:diagnostics",
        "control:cycle",
        "control:mode",
        "control:tests",
        "control:originate",
        "control:audio",
        "control:inserts",
        "control:config",
        "*",
    }
)
SCOPE_RE = re.compile(r"^(?:\*|[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*)$")


@dataclass(frozen=True)
class RouteAuthPolicy:
    scopes: frozenset[str] = frozenset()
    client_credential: bool = False

    @property
    def public(self) -> bool:
        return not self.scopes and not self.client_credential


def _protected(*scopes: str) -> RouteAuthPolicy:
    return RouteAuthPolicy(frozenset(scopes))


PUBLIC_ROUTE = RouteAuthPolicy()
CLIENT_CREDENTIAL_ROUTE = RouteAuthPolicy(client_credential=True)

ROUTE_AUTH_POLICIES: dict[tuple[str, str], RouteAuthPolicy] = {
    ("GET", "/openapi.json"): PUBLIC_ROUTE,
    ("HEAD", "/openapi.json"): PUBLIC_ROUTE,
    ("GET", "/docs"): PUBLIC_ROUTE,
    ("HEAD", "/docs"): PUBLIC_ROUTE,
    ("GET", "/docs/oauth2-redirect"): PUBLIC_ROUTE,
    ("HEAD", "/docs/oauth2-redirect"): PUBLIC_ROUTE,
    ("GET", "/redoc"): PUBLIC_ROUTE,
    ("HEAD", "/redoc"): PUBLIC_ROUTE,
    ("GET", "/healthz"): PUBLIC_ROUTE,
    ("GET", "/readyz"): PUBLIC_ROUTE,
    ("GET", "/metrics"): PUBLIC_ROUTE,
    ("GET", "/v1/health"): _protected("read:health"),
    ("GET", "/v1/status"): _protected("read:status"),
    ("GET", "/v1/version"): _protected("read:status"),
    ("GET", "/v1/handled-alerts"): PUBLIC_ROUTE,
    ("GET", "/v1/station-feed"): _protected("read:alerts"),
    ("GET", "/v1/config/summary"): _protected("read:config"),
    ("GET", "/v1/config/schema"): _protected("read:config"),
    ("GET", "/v1/config/effective"): _protected("read:config"),
    ("POST", "/v1/config/validate"): _protected("control:config"),
    ("GET", "/v1/segments"): _protected("read:status"),
    ("GET", "/v1/segments/{key}"): _protected("read:status"),
    ("POST", "/v1/segments/{key}/refresh"): _protected("control:cycle"),
    ("GET", "/v1/cycle/plan"): _protected("read:status"),
    ("GET", "/v1/cycle/preview"): _protected("read:status"),
    ("GET", "/v1/commands/{command_id}"): _protected("read:status"),
    ("POST", "/v1/auth/token"): CLIENT_CREDENTIAL_ROUTE,
    ("POST", "/v1/auth/revoke"): CLIENT_CREDENTIAL_ROUTE,
    ("POST", "/v1/cycle/rebuild"): _protected("control:cycle"),
    ("POST", "/v1/mode/heightened"): _protected("control:mode"),
    ("DELETE", "/v1/mode/heightened"): _protected("control:mode"),
    ("POST", "/v1/tests/originate"): _protected("control:tests"),
    ("POST", "/v1/uploads/audio"): _protected("control:audio"),
    ("POST", "/v1/inserts/text"): _protected("control:inserts"),
    ("POST", "/v1/inserts/audio"): _protected("control:inserts"),
    ("GET", "/v1/inserts"): _protected("control:inserts"),
    ("GET", "/v1/inserts/{insert_id}"): _protected("control:inserts"),
    ("DELETE", "/v1/inserts/{insert_id}"): _protected("control:inserts"),
    ("POST", "/v1/originate/text"): _protected("control:originate"),
    ("POST", "/v1/originate/audio"): _protected("control:originate"),
    ("POST", "/v1/config/reload"): _protected("control:config"),
    ("GET", "/v1/diagnostics/catalog"): _protected("read:diagnostics"),
    ("GET", "/v1/diagnostics/catalog/{code}"): _protected("read:diagnostics"),
    ("GET", "/v1/diagnostics/active"): _protected("read:diagnostics"),
    ("GET", "/v1/diagnostics/history"): _protected("read:diagnostics"),
    ("GET", "/v1/events"): _protected("read:status"),
}


def scopes_are_write_capable(scopes: frozenset[str]) -> bool:
    if "*" in scopes:
        return True
    write_scopes = {
        scope
        for (method, _), policy in ROUTE_AUTH_POLICIES.items()
        if method not in {"GET", "HEAD"}
        for scope in policy.scopes
    }
    return bool(scopes.intersection(write_scopes))
