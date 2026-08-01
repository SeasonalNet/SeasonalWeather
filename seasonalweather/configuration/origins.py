"""Secret-free value provenance."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .paths import ConfigPath
from .source import RelatedLocation, SourceLocation


class OriginKind(StrEnum):
    FILE = "file"
    ENVIRONMENT = "environment"
    DEFAULT = "default"
    GENERATED = "generated"


@dataclass(frozen=True)
class ValueOrigin:
    path: ConfigPath
    kind: OriginKind
    location: SourceLocation | None = None
    related: tuple[RelatedLocation, ...] = ()
    environment_variable: str | None = None
    declaration_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "related", tuple(self.related))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path.to_pointer(),
            "kind": self.kind.value,
        }
        if self.location:
            result["location"] = self.location.to_dict()
        if self.related:
            result["related"] = [item.to_dict() for item in self.related]
        if self.environment_variable:
            result["environment_variable"] = self.environment_variable
        if self.declaration_id:
            result["declaration_id"] = self.declaration_id
        return result


ENVIRONMENT_BINDINGS: tuple[tuple[ConfigPath, str, str | int | None], ...] = (
    (ConfigPath(("secrets", "nwws_jid")), "NWWS_JID", ""),
    (ConfigPath(("secrets", "nwws_password")), "NWWS_PASSWORD", ""),
    (
        ConfigPath(("secrets", "icecast_source_password")),
        "ICECAST_SOURCE_PASSWORD",
        None,
    ),
    (
        ConfigPath(("secrets", "icecast_admin_password")),
        "ICECAST_ADMIN_PASSWORD",
        "",
    ),
    (
        ConfigPath(("secrets", "icecast_relay_password")),
        "ICECAST_RELAY_PASSWORD",
        "",
    ),
    (ConfigPath(("secrets", "api_token")), "SEASONAL_API_TOKEN", ""),
    (
        ConfigPath(("secrets", "api_tokens_json")),
        "SEASONAL_API_TOKENS_JSON",
        "",
    ),
    (
        ConfigPath(("secrets", "liquidsoap_host")),
        "LIQUIDSOAP_TELNET_HOST",
        "127.0.0.1",
    ),
    (
        ConfigPath(("secrets", "liquidsoap_port")),
        "LIQUIDSOAP_TELNET_PORT",
        1234,
    ),
    (
        ConfigPath(("logs", "discord", "alerts_url")),
        "SEASONAL_DISCORD_ALERTS_WEBHOOK",
        "",
    ),
    (
        ConfigPath(("logs", "discord", "ops_url")),
        "SEASONAL_DISCORD_OPS_WEBHOOK",
        "",
    ),
    (
        ConfigPath(("logs", "discord", "api_url")),
        "SEASONAL_DISCORD_API_WEBHOOK",
        "",
    ),
    (
        ConfigPath(("logs", "discord", "errors_url")),
        "SEASONAL_DISCORD_ERRORS_WEBHOOK",
        "",
    ),
)
