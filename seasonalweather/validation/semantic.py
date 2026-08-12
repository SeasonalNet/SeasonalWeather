"""Central cross-field semantic validation for schema-valid candidates."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from typing import Any, cast

from seasonalweather.configuration.compiler import CompiledConfiguration
from seasonalweather.configuration.paths import ConfigPath
from seasonalweather.configuration.redaction import is_secret_path
from seasonalweather.configuration.semantic_rules import (
    current_and_legacy_auth_conflict,
    exchange_ttls_are_ordered,
    job_repository_identity_errors,
    job_repository_timing_errors,
    lifecycle_timeout_error,
    static_credential_sources_conflict,
)
from seasonalweather.configuration.source import RelatedLocation, SourceLocation
from seasonalweather.diagnostics.bindings import code_for_rule
from seasonalweather.diagnostics.models import DiagnosticSeverity
from seasonalweather.regex_safety import (
    MAX_CONFIGURED_REGEX_PATTERN,
    MAX_CONFIGURED_REGEX_REPLACEMENT,
    MAX_CONFIGURED_REGEX_RULES,
    compile_safe_regex,
    validate_replacement,
)

from .issues import FixOperation, FixSafety, MachineFix, ValidationIssue, ValidationStage
from .paths import DiagnosticPath

_LIFECYCLE_DEFAULTS = {
    "total_seconds": 30.0,
    "active_request_seconds": 10.0,
    "publication_seconds": 8.0,
    "source_stop_seconds": 8.0,
    "tts_stop_seconds": 8.0,
    "task_cancel_seconds": 5.0,
    "resource_close_seconds": 5.0,
}


def _get(root: Mapping[str, object], path: tuple[str, ...], default: object) -> object:
    current: object = root
    for segment in path:
        if not isinstance(current, Mapping) or segment not in current:
            return default
        current = current[segment]
    return current


def _location(compiled: CompiledConfiguration, path: ConfigPath) -> SourceLocation | None:
    if compiled.parsed is None:
        return None
    node = compiled.parsed.locations.get(path)
    return node.value if node else None


def _related(
    compiled: CompiledConfiguration,
    paths: tuple[ConfigPath, ...],
    *,
    excluding: SourceLocation | None,
) -> tuple[RelatedLocation, ...]:
    output: list[RelatedLocation] = []
    for path in paths:
        location = _location(compiled, path)
        if location is not None and location != excluding:
            output.append(RelatedLocation(location, f"related field {path.to_human()}"))
    return tuple(output[:8])


def _semantic_issue(
    compiled: CompiledConfiguration,
    *,
    path: ConfigPath,
    related_paths: tuple[ConfigPath, ...],
    message: str,
    help_text: str,
    validator_rule_id: str,
    fixes: tuple[MachineFix, ...] = (),
) -> ValidationIssue:
    primary = _location(compiled, path)
    return ValidationIssue(
        rule_id="semantic.invariant",
        validator_rule_id=validator_rule_id,
        phase=ValidationStage.SEMANTIC,
        severity=DiagnosticSeverity.ERROR,
        blocking=True,
        message=message,
        path=DiagnosticPath.configuration(path),
        primary=primary,
        related=_related(compiled, related_paths, excluding=primary),
        help=help_text,
        fixes=fixes,
        redacted=is_secret_path(path),
        operational_effect="The candidate cannot be admitted until the contradiction is corrected.",
        documentation_reference="docs/configuration-validation.md",
    )


def validate_semantics(compiled: CompiledConfiguration) -> tuple[ValidationIssue, ...]:
    """Return deterministic source-addressed findings for a valid typed tree."""

    if not compiled.valid or compiled.value is None:
        return ()
    value = compiled.value
    issues: list[ValidationIssue] = []
    issues.extend(_auth_mode_issues(compiled, value))
    issues.extend(_auth_ttl_issues(compiled, value))
    issues.extend(_job_issues(compiled, value))
    issues.extend(_lifecycle_issues(compiled, value))
    issues.extend(_tts_issues(compiled, value))
    return tuple(sorted(issues, key=ValidationIssue.sort_key))


_TTS_BACKENDS = frozenset({"local", "seasonal_ttsd", "openai_compatible"})
_TTS_LOCAL_ALIASES = frozenset({"espeak", "espeak-ng", "espeak_ng", "piper", "festival", "dectalk", "voicetext_paul"})


def _tts_issues(compiled: CompiledConfiguration, value: Mapping[str, object]) -> tuple[ValidationIssue, ...]:
    tts = _get(value, ("tts",), {})
    if not isinstance(tts, Mapping):
        return ()
    return _tts_backend_issues(compiled, tts) + _tts_local_issues(compiled, tts) + _tts_regex_issues(compiled, tts)


def _tts_regex_issues(compiled: CompiledConfiguration, tts: Mapping[str, object]) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    issues.extend(
        _configured_regex_rule_issues(
            compiled,
            _mapping_value(tts, "text_overrides"),
            base=("tts", "text_overrides"),
            replacement_key="replace",
            label="text override",
        )
    )
    local = _mapping_value(tts, "local")
    local_vtp = _mapping_value(local, "voicetext_paul")
    legacy_vtp = _mapping_value(tts, "voicetext_paul")
    for vtp, base in ((local_vtp, ("tts", "local", "voicetext_paul")), (legacy_vtp, ("tts", "voicetext_paul"))):
        issues.extend(
            _configured_regex_rule_issues(
                compiled,
                _mapping_value(vtp, "alias_overrides"),
                base=(*base, "alias_overrides"),
                replacement_key="alias",
                label="VoiceText alias override",
            )
        )
        issues.extend(
            _configured_regex_rule_issues(
                compiled,
                _mapping_value(vtp, "phoneme_overrides_x_cmu"),
                base=(*base, "phoneme_overrides_x_cmu"),
                replacement_key="ph",
                label="VoiceText phoneme override",
            )
        )
    return tuple(issues)


def _mapping_value(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, Mapping) else None


def _configured_regex_rule_issues(
    compiled: CompiledConfiguration,
    raw_rules: object,
    *,
    base: tuple[str | int, ...],
    replacement_key: str,
    label: str,
) -> tuple[ValidationIssue, ...]:
    if raw_rules is None:
        return ()
    if not isinstance(raw_rules, (list, tuple)):
        return (
            _tts_issue(
                compiled,
                base,
                f"{label} list is invalid.",
                "Use a bounded list of override objects.",
                "semantic.invariant",
            ),
        )
    issues: list[ValidationIssue] = []
    if len(raw_rules) > MAX_CONFIGURED_REGEX_RULES:
        issues.append(_regex_count_issue(compiled, base, label))
    for index, raw in enumerate(raw_rules[:MAX_CONFIGURED_REGEX_RULES]):
        issues.extend(
            _configured_regex_rule_issue(
                compiled,
                raw,
                path=(*base, index),
                replacement_key=replacement_key,
                label=label,
            )
        )
    return tuple(issues)


def _regex_count_issue(compiled: CompiledConfiguration, base: tuple[str | int, ...], label: str) -> ValidationIssue:
    return _tts_issue(
        compiled,
        base,
        f"{label} count exceeds the bounded limit.",
        f"Use at most {MAX_CONFIGURED_REGEX_RULES} configured rules.",
        "semantic.invariant",
    )


def _configured_regex_rule_issue(
    compiled: CompiledConfiguration,
    raw: object,
    *,
    path: tuple[str | int, ...],
    replacement_key: str,
    label: str,
) -> tuple[ValidationIssue, ...]:
    if not isinstance(raw, Mapping):
        return (
            _tts_issue(
                compiled,
                path,
                f"{label} must be an object.",
                "Use the documented override shape.",
                "semantic.invariant",
            ),
        )
    match = raw.get("match")
    replacement = raw.get(replacement_key)
    issues = list(_regex_field_issues(compiled, path, match, replacement, replacement_key, label))
    if bool(raw.get("regex", False)) and isinstance(match, str) and match:
        issues.extend(
            _regex_match_issues(
                compiled,
                path,
                match,
                replacement,
                replacement_key,
                label,
                ignore_case=bool(raw.get("ignore_case", False)),
            )
        )
    return tuple(issues)


def _regex_field_issues(
    compiled: CompiledConfiguration,
    path: tuple[str | int, ...],
    match: object,
    replacement: object,
    replacement_key: str,
    label: str,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if not isinstance(match, str) or not match or len(match) > MAX_CONFIGURED_REGEX_PATTERN or "\x00" in match:
        issues.append(
            _tts_issue(
                compiled,
                (*path, "match"),
                f"{label} pattern is invalid or overlong.",
                "Use a bounded non-empty pattern.",
                "semantic.invariant",
            )
        )
    if not isinstance(replacement, str) or len(replacement) > MAX_CONFIGURED_REGEX_REPLACEMENT or "\x00" in replacement:
        issues.append(
            _tts_issue(
                compiled,
                (*path, replacement_key),
                f"{label} replacement is invalid or overlong.",
                "Use a bounded replacement string.",
                "semantic.invariant",
            )
        )
    elif isinstance(replacement, str):
        try:
            validate_replacement(replacement)
        except ValueError:
            issues.append(
                _tts_issue(
                    compiled,
                    (*path, replacement_key),
                    f"{label} replacement uses an unsupported backreference or Python escape.",
                    "Use a literal replacement without backreferences.",
                    "semantic.invariant",
                )
            )
    return tuple(issues)


def _regex_match_issues(
    compiled: CompiledConfiguration,
    path: tuple[str | int, ...],
    match: str,
    replacement: object,
    replacement_key: str,
    label: str,
    *,
    ignore_case: bool,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    try:
        compile_safe_regex(match, flags=re.IGNORECASE if ignore_case else 0)
    except ValueError as exc:
        issues.append(
            _tts_issue(
                compiled,
                (*path, "match"),
                f"{label} regex is not in the safe P1-16 grammar: {str(exc)[:160]}.",
                "Use a bounded literal or the documented safe regex subset.",
                "semantic.invariant",
            )
        )
    return tuple(issues)


def _tts_issue(
    compiled: CompiledConfiguration,
    path: tuple[str | int, ...],
    message: str,
    help_text: str,
    rule: str,
) -> ValidationIssue:
    return _semantic_issue(
        compiled,
        path=ConfigPath(path),
        related_paths=(),
        message=message,
        help_text=help_text,
        validator_rule_id=rule,
    )


def _tts_backend_issues(compiled: CompiledConfiguration, tts: Mapping[str, object]) -> tuple[ValidationIssue, ...]:
    backend = str(tts.get("backend", "")).strip().lower()
    canonical_primary = "local" if backend in _TTS_LOCAL_ALIASES else backend
    issues: list[ValidationIssue] = []
    if backend not in _TTS_BACKENDS and backend not in _TTS_LOCAL_ALIASES:
        issues.append(
            _tts_issue(
                compiled,
                ("tts", "backend"),
                "TTS backend identity is unknown.",
                "Use local, seasonal_ttsd, or openai_compatible.",
                "semantic.invariant",
            )
        )
    return tuple(issues) + _tts_fallback_issues(compiled, tts, canonical_primary)


def _tts_fallback_issues(
    compiled: CompiledConfiguration, tts: Mapping[str, object], canonical_primary: str
) -> tuple[ValidationIssue, ...]:
    fallback_raw = tts.get("fallback_backend")
    fallback = None if fallback_raw is None else str(fallback_raw).strip().lower()
    canonical_fallback = "local" if fallback in _TTS_LOCAL_ALIASES else fallback
    message = _tts_fallback_message(fallback, canonical_fallback, canonical_primary)
    return () if message is None else (_tts_issue(compiled, ("tts", "fallback_backend"), *message),)


def _tts_fallback_message(
    fallback: str | None, canonical_fallback: str | None, canonical_primary: str
) -> tuple[str, str, str] | None:
    if fallback is not None and fallback not in _TTS_BACKENDS and fallback not in _TTS_LOCAL_ALIASES:
        return "TTS fallback identity is unknown.", "Choose a known backend.", "semantic.invariant"
    if canonical_fallback == canonical_primary:
        return (
            "TTS fallback cannot refer to the primary backend.",
            "Choose a different backend or omit fallback.",
            "semantic.invariant",
        )
    if canonical_primary == "local" and canonical_fallback is not None:
        return (
            "P1-16 local synthesis cannot fall back to a deferred remote backend.",
            "Use no fallback for local or select a remote primary with local fallback.",
            "semantic.invariant",
        )
    if canonical_primary in {"seasonal_ttsd", "openai_compatible"} and canonical_fallback not in {None, "local"}:
        return (
            "P1-16 only remote-known-primary to local fallback is meaningful.",
            "Defer remote-to-remote fallback until P1-17.",
            "semantic.invariant",
        )
    return None


def _tts_local_issues(compiled: CompiledConfiguration, tts: Mapping[str, object]) -> tuple[ValidationIssue, ...]:
    local = tts.get("local", {})
    if not isinstance(local, Mapping):
        return ()
    engine = str(local.get("engine", "espeak-ng")).strip().lower()
    return _tts_engine_issues(compiled, engine) + _tts_control_issues(compiled, tts, local)


def _tts_engine_issues(compiled: CompiledConfiguration, engine: str) -> tuple[ValidationIssue, ...]:
    if engine not in _TTS_LOCAL_ALIASES:
        return (
            _tts_issue(
                compiled,
                ("tts", "local", "engine"),
                "Local TTS engine identity is unsupported.",
                "Choose a registered local engine.",
                "semantic.invariant",
            ),
        )
    return ()


def _tts_control_issues(
    compiled: CompiledConfiguration, tts: Mapping[str, object], local: Mapping[str, object]
) -> tuple[ValidationIssue, ...]:
    return _tts_voice_rate_issues(compiled, tts, local) + _tts_volume_issues(compiled, tts)


def _tts_voice_rate_issues(
    compiled: CompiledConfiguration, tts: Mapping[str, object], local: Mapping[str, object]
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    voice = str(local.get("voice", tts.get("voice", "9")))
    rate = local.get("rate_wpm", tts.get("rate_wpm", 165))
    if not voice or len(voice) > 128 or any(char in voice for char in "\x00\r\n"):
        issues.append(
            _tts_issue(
                compiled,
                ("tts", "local", "voice"),
                "Local TTS voice control is invalid.",
                "Use a bounded voice/profile identifier.",
                "semantic.invariant",
            )
        )
    if not isinstance(rate, int) or isinstance(rate, bool) or not 40 <= rate <= 600:
        issues.append(
            _tts_issue(
                compiled,
                ("tts", "local", "rate_wpm"),
                "Local TTS rate is outside its supported range.",
                "Use a rate from 40 through 600 WPM.",
                "semantic.invariant",
            )
        )
    return tuple(issues)


def _tts_volume_issues(compiled: CompiledConfiguration, tts: Mapping[str, object]) -> tuple[ValidationIssue, ...]:
    volume = tts.get("volume", 1.0)
    if not isinstance(volume, (int, float)) or isinstance(volume, bool) or not 0.0 <= float(volume) <= 2.0:
        return (
            _tts_issue(
                compiled,
                ("tts", "volume"),
                "TTS volume control is outside its supported range.",
                "Use a volume from 0.0 through 2.0.",
                "semantic.invariant",
            ),
        )
    return ()


def _auth_mode_issues(
    compiled: CompiledConfiguration,
    value: Mapping[str, object],
) -> tuple[ValidationIssue, ...]:
    api = _get(value, ("api",), {})
    auth_present = isinstance(api, Mapping) and "auth" in api
    legacy_fields = {name for name in ("subject", "scopes") if name in api} if isinstance(api, Mapping) else set()
    legacy_issue = _legacy_auth_issue(
        compiled,
        auth_present=auth_present,
        legacy_fields=legacy_fields,
    )
    present_environment = {
        origin.environment_variable for origin in compiled.report.origins if origin.environment_variable
    }
    single = "SEASONAL_API_TOKEN" in present_environment
    multiple = "SEASONAL_API_TOKENS_JSON" in present_environment
    mode = str(_get(value, ("api", "auth", "mode"), "static"))
    credential_issue = _credential_source_issue(
        compiled,
        single=single,
        multiple=multiple,
        mode=mode,
    )
    return tuple(issue for issue in (legacy_issue, credential_issue) if issue is not None)


def _legacy_auth_issue(
    compiled: CompiledConfiguration,
    *,
    auth_present: bool,
    legacy_fields: set[str],
) -> ValidationIssue | None:
    if not current_and_legacy_auth_conflict(
        auth_present=auth_present,
        legacy_fields=legacy_fields,
    ):
        return None
    return _semantic_issue(
        compiled,
        path=ConfigPath(("api", "auth")),
        related_paths=tuple(ConfigPath(("api", name)) for name in sorted(legacy_fields)),
        message="Current and legacy API authentication fields cannot be combined.",
        help_text="Keep api.auth and remove the legacy api.subject/api.scopes fields.",
        validator_rule_id="semantic.auth.current_legacy",
    )


def _credential_source_issue(
    compiled: CompiledConfiguration,
    *,
    single: bool,
    multiple: bool,
    mode: str,
) -> ValidationIssue | None:
    if static_credential_sources_conflict("present" if single else "", "present" if multiple else ""):
        return ValidationIssue(
            rule_id="semantic.invariant",
            validator_rule_id="semantic.auth.static_sources",
            phase=ValidationStage.SEMANTIC,
            severity=DiagnosticSeverity.ERROR,
            blocking=True,
            message="Exactly one static API credential source may be present.",
            path=DiagnosticPath.configuration(ConfigPath(("secrets", "api_token"))),
            notes=(
                "SEASONAL_API_TOKEN is present.",
                "SEASONAL_API_TOKENS_JSON is present.",
            ),
            help="Remove one credential source without exposing either value.",
            redacted=True,
            documentation_reference="docs/configuration-validation.md",
        )
    if mode == "exchange" and (single or multiple):
        return _semantic_issue(
            compiled,
            path=ConfigPath(("api", "auth", "mode")),
            related_paths=(),
            message="Exchange-only authentication cannot include a static credential source.",
            help_text="Remove the static credential environment binding or select hybrid mode.",
            validator_rule_id="semantic.auth.exchange_static",
        )
    return None


def _auth_ttl_issues(
    compiled: CompiledConfiguration,
    value: Mapping[str, object],
) -> tuple[ValidationIssue, ...]:
    names = (
        "minimum_ttl_seconds",
        "default_ttl_seconds",
        "maximum_write_ttl_seconds",
        "maximum_read_ttl_seconds",
    )
    defaults = (60, 900, 900, 3600)
    values = tuple(
        int(cast(Any, _get(value, ("api", "auth", "exchange", name), default)))
        for name, default in zip(names, defaults, strict=True)
    )
    if exchange_ttls_are_ordered(*values):
        return ()
    paths = tuple(ConfigPath(("api", "auth", "exchange", name)) for name in names)
    return (
        _semantic_issue(
            compiled,
            path=paths[0],
            related_paths=paths[1:],
            message="Exchange token TTL values must be positive and monotonically ordered.",
            help_text=(
                "Set minimum_ttl_seconds <= default_ttl_seconds <= "
                "maximum_write_ttl_seconds <= maximum_read_ttl_seconds."
            ),
            validator_rule_id="semantic.auth.exchange_ttl_order",
        ),
    )


def _job_issues(
    compiled: CompiledConfiguration,
    value: Mapping[str, object],
) -> tuple[ValidationIssue, ...]:
    enabled = bool(_get(value, ("jobs", "enabled"), False))
    required = bool(_get(value, ("jobs", "required"), False))
    job_path = str(_get(value, ("jobs", "path"), "")).strip()
    database_path = str(_get(value, ("database", "path"), "")).strip()
    work_dir = str(_get(value, ("paths", "work_dir"), ""))
    effective_database_path = database_path or posixpath.join(
        work_dir.replace("\\", "/"),
        "seasonalweather.sqlite3",
    )
    ack = int(cast(Any, _get(value, ("jobs", "assignment_ack_seconds"), 10)))
    lease = int(cast(Any, _get(value, ("jobs", "lease_seconds"), 60)))
    output: list[ValidationIssue] = []
    identity_errors = job_repository_identity_errors(
        enabled=enabled,
        required=required,
        path=job_path,
        operational_database_path=effective_database_path,
    )
    if "jobs.required cannot be true when jobs are disabled" in identity_errors:
        output.append(
            _semantic_issue(
                compiled,
                path=ConfigPath(("jobs", "required")),
                related_paths=(ConfigPath(("jobs", "enabled")),),
                message="A required job repository cannot be disabled.",
                help_text="Enable jobs or set jobs.required to false.",
                validator_rule_id="semantic.jobs.required_enabled",
            )
        )
    if "jobs.path must be explicitly configured when jobs are enabled" in identity_errors:
        output.append(
            _semantic_issue(
                compiled,
                path=ConfigPath(("jobs", "path")),
                related_paths=(ConfigPath(("jobs", "enabled")),),
                message="An enabled job repository requires an explicit path.",
                help_text="Set jobs.path to a dedicated SQLite database path.",
                validator_rule_id="semantic.jobs.path_required",
            )
        )
    if "jobs.path must be separate from database.path" in identity_errors:
        output.append(
            _semantic_issue(
                compiled,
                path=ConfigPath(("jobs", "path")),
                related_paths=(
                    ConfigPath(("database", "path")),
                    ConfigPath(("paths", "work_dir")),
                ),
                message="The job repository and operational database must use separate paths.",
                help_text="Choose a dedicated jobs.path outside the operational database file.",
                validator_rule_id="semantic.jobs.path_distinct",
            )
        )
    timing_errors = job_repository_timing_errors(
        busy_timeout_ms=int(cast(Any, _get(value, ("jobs", "busy_timeout_ms"), 5000))),
        assignment_ack_seconds=ack,
        lease_seconds=lease,
        shutdown_reconciliation_seconds=float(cast(Any, _get(value, ("jobs", "shutdown_reconciliation_seconds"), 5.0))),
    )
    if any(error.startswith("jobs lease timing") for error in timing_errors):
        output.append(
            _semantic_issue(
                compiled,
                path=ConfigPath(("jobs", "assignment_ack_seconds")),
                related_paths=(ConfigPath(("jobs", "lease_seconds")),),
                message="Job acknowledgment and lease timing is impossible.",
                help_text="Use 1 <= assignment_ack_seconds < lease_seconds <= 3600.",
                validator_rule_id="semantic.jobs.lease_timing",
            )
        )
    return tuple(output)


def _lifecycle_issues(
    compiled: CompiledConfiguration,
    value: Mapping[str, object],
) -> tuple[ValidationIssue, ...]:
    values = {
        name: float(cast(Any, _get(value, ("lifecycle", name), default)))
        for name, default in _LIFECYCLE_DEFAULTS.items()
    }
    paths = {name: ConfigPath(("lifecycle", name)) for name in values}
    error = lifecycle_timeout_error(
        total_seconds=values["total_seconds"],
        stage_seconds=tuple(values[name] for name in tuple(values)[1:]),
    )
    if error == "lifecycle timeout values must be positive":
        return (_nonpositive_lifecycle_issue(compiled, values, paths),)
    largest_name = max(tuple(values)[1:], key=values.__getitem__)
    if values["total_seconds"] >= values[largest_name]:
        return ()
    return (_short_total_lifecycle_issue(compiled, values, paths, largest_name),)


def _nonpositive_lifecycle_issue(
    compiled: CompiledConfiguration,
    values: Mapping[str, float],
    paths: Mapping[str, ConfigPath],
) -> ValidationIssue:
    invalid = next(name for name, item in values.items() if item <= 0)
    return _semantic_issue(
        compiled,
        path=paths[invalid],
        related_paths=tuple(paths[name] for name in values if name != invalid),
        message="Lifecycle timeout values must all be positive.",
        help_text="Set every lifecycle timeout to a positive number of seconds.",
        validator_rule_id="semantic.lifecycle.positive",
    )


def _short_total_lifecycle_issue(
    compiled: CompiledConfiguration,
    values: Mapping[str, float],
    paths: Mapping[str, ConfigPath],
    largest_name: str,
) -> ValidationIssue:
    total_path = paths["total_seconds"]
    source_sha256 = compiled.source.digest if compiled.source else None
    fix = MachineFix(
        operation=FixOperation.REPLACE,
        target=DiagnosticPath.configuration(total_path),
        diagnostic_code=code_for_rule("semantic.invariant"),
        safety=FixSafety.SAFE,
        replacement=values[largest_name],
        expected_old_value=values["total_seconds"],
        expected_source_sha256=source_sha256,
        applicability=("The stage timeout values are otherwise unchanged.",),
        location=_location(compiled, total_path),
    )
    return _semantic_issue(
        compiled,
        path=total_path,
        related_paths=(paths[largest_name],),
        message="The total lifecycle deadline is shorter than a stage timeout.",
        help_text=f"Set lifecycle.total_seconds to at least {values[largest_name]:g}.",
        validator_rule_id="semantic.lifecycle.total_covers_stage",
        fixes=(fix,),
    )
