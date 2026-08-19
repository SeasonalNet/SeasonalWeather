"""Safe API projections for configuration schema, validation, and runtime state."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from enum import Enum
from pathlib import PurePath
from urllib.parse import urlsplit, urlunsplit

from seasonalweather.config import AppConfig
from seasonalweather.configuration.compiler import compile_path
from seasonalweather.configuration.origins import ENVIRONMENT_BINDINGS
from seasonalweather.configuration.schema import public_schema_document
from seasonalweather.validation import (
    EnvironmentInputIdentity,
    ValidationContext,
    ValidationPolicy,
    configured_preflight_probes,
    validate_compiled,
)


def configuration_schema() -> dict[str, object]:
    return public_schema_document()


def effective_configuration(config: AppConfig) -> dict[str, object]:
    """Serialize effective typed configuration without secrets or local paths."""

    return {
        "config_schema": 1,
        "configuration": _safe_value(config, ()),
        "redacted": True,
    }


async def validate_configuration(
    config_path: str,
    *,
    preflight: bool = False,
    warnings_as_errors: bool = False,
) -> dict[str, object]:
    compiled = compile_path(config_path)
    environment_inputs = tuple(
        EnvironmentInputIdentity(
            variable=variable,
            present=bool(os.environ.get(variable, "")),
        )
        for _path, variable, _default in ENVIRONMENT_BINDINGS
    )
    report = await validate_compiled(
        compiled,
        context=ValidationContext(
            preflight_enabled=preflight,
            preflight_probes=configured_preflight_probes(compiled) if preflight else (),
            environment_inputs=environment_inputs,
            policy=ValidationPolicy(warning_blocks=warnings_as_errors),
        ),
    )
    return {
        "valid": report.decision.valid,
        "preflight_ready": report.decision.preflight_ready,
        "report": _redact_validation(report.to_dict()),
        "redacted": True,
    }


def _safe_value(value: object, path: tuple[str, ...]) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result: dict[str, object] = {}
        for field in dataclasses.fields(value):
            result[field.name] = _safe_field(field.name, getattr(value, field.name), path)
        return result
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _safe_value(item, (*path, str(key)))
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item, path) for item in value]
    if isinstance(value, PurePath):
        return {"configured": bool(str(value))}
    return value


def _safe_field(name: str, value: object, path: tuple[str, ...]) -> object:
    lowered = name.casefold()
    field_path = (*path, name)
    if "secret" in path or _is_sensitive_name(lowered):
        if lowered == "credentials" and isinstance(value, (list, tuple)):
            return [
                {
                    "subject": getattr(item, "subject", None),
                    "scopes": sorted(getattr(item, "scopes", ())),
                    "configured": bool(getattr(item, "token", "")),
                }
                for item in value
            ]
        if isinstance(value, (list, tuple, set, frozenset)):
            return {"configured": bool(value), "count": len(value)}
        return {"configured": bool(value)}
    if _is_local_path_name(lowered):
        return {"configured": bool(value)}
    if isinstance(value, str) and _looks_like_url(value):
        return _safe_url(value)
    return _safe_value(value, field_path)


def _is_sensitive_name(name: str) -> bool:
    return any(term in name for term in ("password", "token", "secret", "credential", "api_key"))


def _is_local_path_name(name: str) -> bool:
    return name == "path" or name.endswith(("_path", "_dir", "_file")) or name in {"workdir", "directory"}


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return "<configured-url>"
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    except ValueError:
        return "<configured-url>"


def _redact_validation(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): ("<configured-source>" if str(key) in {"source", "source_id"} else _redact_validation(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_validation(item) for item in value]
    return value
