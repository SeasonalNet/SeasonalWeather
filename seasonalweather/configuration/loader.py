"""Narrow startup adapter from compiled values to runtime AppConfig."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from .compiler import CompiledConfiguration, compile_path
from .environment import EnvironmentValues
from .issues import CompileIssue
from .renderer import render_report

if TYPE_CHECKING:
    from seasonalweather.config import AppConfig


class ConfigurationCompileError(ValueError):
    """Bounded parse/schema startup failure."""

    def __init__(self, compiled: CompiledConfiguration) -> None:
        self.report = compiled.report
        source = (compiled.source,) if compiled.source else ()
        super().__init__(render_report(compiled.report, sources=source))


def load_runtime_config(
    path: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    effective_environment = os.environ if environ is None else environ
    compiled = compile_path(path, environ=effective_environment)
    if not compiled.valid or compiled.value is None:
        _raise_legacy_auth_error(compiled)
        raise ConfigurationCompileError(compiled)
    from seasonalweather.config import _build_app_config

    return _build_app_config(
        dict(compiled.value),
        environment=EnvironmentValues(effective_environment),
    )


def _raise_legacy_auth_error(compiled: CompiledConfiguration) -> None:
    """Preserve the established public exception for api.auth.mode failures."""
    issue = _auth_mode_issue(compiled)
    if issue is None:
        return
    from seasonalweather.config import AuthConfigurationError

    raise AuthConfigurationError(
        kind=_legacy_auth_error_kind(issue.rule_id, _auth_mode_value(compiled)),
        path="api.auth.mode",
        message="Authentication mode configuration is invalid.",
    )


def _auth_mode_issue(compiled: CompiledConfiguration) -> CompileIssue | None:
    return next(
        (
            item
            for item in compiled.report.issues
            if item.path is not None and item.path.to_pointer() == "/api/auth/mode"
        ),
        None,
    )


def _auth_mode_value(compiled: CompiledConfiguration) -> object:
    if compiled.parsed is None:
        return None
    api = compiled.parsed.value.get("api")
    if not isinstance(api, dict):
        return None
    auth = api.get("auth")
    return auth.get("mode") if isinstance(auth, dict) else None


def _legacy_auth_error_kind(rule_id: str, value: object) -> str:
    if rule_id == "schema.required":
        return "missing_value"
    if rule_id == "schema.type":
        return "invalid_type"
    return "empty_value" if isinstance(value, str) and not value.strip() else "unknown_value"
