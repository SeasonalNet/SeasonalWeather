"""Fail-closed verification of externally supplied validation-report mappings."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from seasonalweather.configuration.origins import ENVIRONMENT_BINDINGS
from seasonalweather.diagnostics.bindings import code_for_rule
from seasonalweather.diagnostics.models import DiagnosticSeverity

from .candidate_identity import canonical_report_sha256, complete_candidate_sha256, source_manifest_sha256
from .compatibility import (
    CompatibilityIdentity,
    SupportedCompatibility,
    analyze_compatibility,
)
from .issues import STAGE_ORDER, FixOperation, FixSafety, StageState, ValidationStage
from .limits import VALIDATION_ENVELOPE_SECONDS
from .paths import DiagnosticPath, PathKind
from .preflight import (
    ProbeFailureKind,
    ProbeRedaction,
    ProbeStatus,
    canonical_probe_summary,
    safe_probe_evidence,
)
from .rules import expected_rule_identities, issue_contract, validate_rule_binding

MALFORMED = "malformed_report"
UNSUPPORTED = "unsupported_report"
CANDIDATE_MISMATCH = "candidate_hash_mismatch"
STALE = "stale_active_generation"
INCOMPATIBLE = "incompatible_validator_stamp"
CONTRADICTORY = "contradictory_report"
REPORT_MISMATCH = "report_binding_mismatch"

_MAX_DEPTH = 16
_MAX_NODES = 12_000
_MAX_STRING = 2_048
_MAX_ISSUES = 256
_MAX_PROBES = 64
_MAX_MANIFEST = 512
_SEVERITY_ORDER = {
    DiagnosticSeverity.INFO.value: 0,
    DiagnosticSeverity.SUGGESTION.value: 1,
    DiagnosticSeverity.DEPRECATION.value: 2,
    DiagnosticSeverity.WARNING.value: 3,
    DiagnosticSeverity.ERROR.value: 4,
}


class _Rejected(ValueError):
    def __init__(self, kind: str = MALFORMED) -> None:
        self.kind = kind
        super().__init__(kind)


@dataclass
class _Observed:
    stages: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    probes: list[dict[str, Any]] = field(default_factory=list)


def verify_external_mapping(
    payload: Mapping[str, object],
    *,
    expected_candidate_sha256: str | None,
    expected_candidate_identity_sha256: str,
    expected_report_sha256: str,
    current_active_generation: int | None,
    require_fresh_generation: bool,
    supported: SupportedCompatibility,
    report_version: int,
    stamp_version: int,
    validation_protocol_version: int,
) -> tuple[str, ...]:
    """Validate every public field and recompute all admission decisions."""

    failures: list[str] = []
    try:
        _bounded_json(payload)
        expected_report = _sha256(expected_report_sha256)
        if canonical_report_sha256(payload) != expected_report:
            raise _Rejected(REPORT_MISMATCH)
        _exact_keys(
            payload,
            {
                "validation_report_version",
                "valid",
                "preflight_ready",
                "parse_valid",
                "schema_valid",
                "issues",
                "candidate",
                "validator_stamp",
                "stages",
                "policy",
                "summary",
            },
        )
        supplied_report_version = _integer(payload["validation_report_version"], minimum=1)
        if supplied_report_version != report_version:
            failures.append(UNSUPPORTED)
        candidate = _candidate(
            payload["candidate"],
            expected_candidate_sha256,
            expected_candidate_identity_sha256,
        )
        stamp = _stamp(
            payload["validator_stamp"],
            candidate=candidate,
            supported=supported,
            stamp_version=stamp_version,
            validation_protocol_version=validation_protocol_version,
            report_version=report_version,
        )
        source_fence = (
            candidate["source_manifest"][0]["sha256"] if len(candidate["source_manifest"]) == 1 else candidate["sha256"]
        )
        observed = _stages(payload["stages"], candidate_sha256=source_fence)
        _stage_dependencies(observed.stages)
        _validate_candidate_stage_schema(candidate, observed)
        _stamp_stage_bindings(stamp, observed)
        _reconcile_preflight(observed)
        policy = _policy(payload["policy"])
        decision = _decision(observed, policy)
        _top_level(payload, observed, decision)
        _summary(payload["summary"], decision)
        if require_fresh_generation and (
            current_active_generation is None or stamp["active_configuration_generation"] != current_active_generation
        ):
            failures.append(STALE)
    except _Rejected as exc:
        failures.append(exc.kind)
    except (KeyError, TypeError, ValueError, OverflowError):
        failures.append(MALFORMED)
    return tuple(dict.fromkeys(failures))


def _bounded_json(value: object) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_NODES or depth > _MAX_DEPTH:
            raise _Rejected()
        _bound_item(item, depth, stack)


def _bound_item(
    item: object,
    depth: int,
    stack: list[tuple[object, int]],
) -> None:
    if isinstance(item, str):
        _bound_string(item)
        return
    if item is None or type(item) in {bool, int, float}:
        _bound_number(item)
        return
    if isinstance(item, Mapping):
        _bound_mapping(item, depth, stack)
        return
    if isinstance(item, list):
        _bound_list(item, depth, stack)
        return
    raise _Rejected()


def _bound_string(value: str) -> None:
    if len(value) > _MAX_STRING or any(ord(character) < 32 for character in value):
        raise _Rejected()


def _bound_number(value: object) -> None:
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**63 - 1:
        raise _Rejected()
    if isinstance(value, float) and (value != value or abs(value) == float("inf")):
        raise _Rejected()


def _bound_mapping(
    value: Mapping[object, object],
    depth: int,
    stack: list[tuple[object, int]],
) -> None:
    if len(value) > _MAX_MANIFEST:
        raise _Rejected()
    for key, child in value.items():
        if not isinstance(key, str) or len(key) > 128:
            raise _Rejected()
        stack.append((child, depth + 1))


def _bound_list(
    value: list[object],
    depth: int,
    stack: list[tuple[object, int]],
) -> None:
    if len(value) > _MAX_MANIFEST:
        raise _Rejected()
    stack.extend((child, depth + 1) for child in value)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _Rejected()
    return value


def _list(value: object, *, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise _Rejected()
    return value


def _exact_keys(
    value: Mapping[str, object],
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    keys = set(value)
    allowed = required | (optional or set())
    if not required.issubset(keys) or not keys.issubset(allowed):
        raise _Rejected()


def _string(value: object, *, maximum: int = 512, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise _Rejected()
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise _Rejected()
    return value


def _integer(value: object, *, minimum: int = 0, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _Rejected()
    return value


def _sha256(value: object, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise _Rejected()
    return value


def _candidate(
    value: object,
    expected_sha256: str | None,
    expected_identity_sha256: str,
) -> dict[str, Any]:
    expected = _sha256(expected_sha256, nullable=True)
    expected_identity = _sha256(expected_identity_sha256)
    candidate = _mapping(value)
    _exact_keys(
        candidate,
        {
            "sha256",
            "identity_sha256",
            "config_schema_version",
            "source_manifest",
            "origin_manifest",
            "environment_inputs",
            "reproducible",
        },
    )
    candidate_sha = _sha256(candidate["sha256"], nullable=True)
    identity_sha = _sha256(candidate["identity_sha256"])
    if candidate_sha != expected:
        raise _Rejected(CANDIDATE_MISMATCH)
    schema = _integer(candidate["config_schema_version"], minimum=1, nullable=True)
    sources = _source_manifest(candidate["source_manifest"])
    origins = _origin_manifest(candidate["origin_manifest"], schema)
    environment = _environment_manifest(candidate["environment_inputs"])
    reproducible = _boolean(candidate["reproducible"])
    _validate_candidate_manifests(
        candidate_sha,
        sources,
        environment,
        reproducible=reproducible,
    )
    recomputed = complete_candidate_sha256(
        source_manifest=sources,
        config_schema_version=schema,
        origin_manifest=origins,
        environment_inputs=environment,
    )
    if identity_sha != recomputed or identity_sha != expected_identity:
        raise _Rejected(CANDIDATE_MISMATCH)
    return {
        "sha256": candidate_sha,
        "identity_sha256": identity_sha,
        "config_schema_version": schema,
        "source_manifest": sources,
        "origin_manifest": origins,
        "environment_inputs": environment,
        "reproducible": reproducible,
    }


def _validate_candidate_manifests(
    candidate_sha: str | None,
    sources: list[dict[str, Any]],
    environment: list[dict[str, Any]],
    *,
    reproducible: bool,
) -> None:
    source_ready = _sources_reproducible(sources)
    environment_ready = _environment_reproducible(environment)
    if reproducible != (candidate_sha is not None and source_ready and environment_ready):
        raise _Rejected(CONTRADICTORY)
    if source_manifest_sha256(sources) != candidate_sha:
        raise _Rejected(CONTRADICTORY)


def _sources_reproducible(sources: list[dict[str, Any]]) -> bool:
    return bool(sources) and all(
        item["bytes_available"] and item["sha256"] is not None and item["byte_length"] is not None for item in sources
    )


def _environment_reproducible(environment: list[dict[str, Any]]) -> bool:
    return all(not item["present"] or item["opaque_change_identity"] is not None for item in environment)


def _source_manifest(value: object) -> list[dict[str, Any]]:
    items = _list(value, maximum=64)
    output: list[dict[str, Any]] = []
    for raw in items:
        item = _mapping(raw)
        _exact_keys(item, {"source", "sha256", "byte_length", "bytes_available"})
        source = _string(item["source"], maximum=512)
        digest = _sha256(item["sha256"], nullable=True)
        byte_length = _integer(item["byte_length"], nullable=True)
        available = _boolean(item["bytes_available"])
        if available != (digest is not None and byte_length is not None) or "\x00" in source:
            raise _Rejected(CONTRADICTORY)
        output.append(
            {
                "source": source,
                "sha256": digest,
                "byte_length": byte_length,
                "bytes_available": available,
            }
        )
    if [item["source"] for item in output] != sorted({item["source"] for item in output}):
        raise _Rejected(CONTRADICTORY)
    return output


def _origin_manifest(value: object, schema: int | None) -> list[dict[str, str]]:
    items = _list(value, maximum=512)
    output: list[dict[str, str]] = []
    for raw in items:
        item = _mapping(raw)
        _exact_keys(item, {"path", "kind", "declaration_id"})
        path = _string(item["path"], maximum=512, allow_empty=True)
        kind = _string(item["kind"], maximum=16)
        declaration = _string(item["declaration_id"], maximum=256)
        DiagnosticPath.json_pointer(path)
        if kind not in {"default", "generated"}:
            raise _Rejected()
        if not _valid_origin_declaration(path, kind, declaration, schema):
            raise _Rejected(CONTRADICTORY)
        output.append({"path": path, "kind": kind, "declaration_id": declaration})
    keys = [(item["path"], item["kind"]) for item in output]
    if keys != sorted(set(keys)):
        raise _Rejected(CONTRADICTORY)
    return output


def _valid_origin_declaration(path: str, kind: str, declaration: str, schema: int | None) -> bool:
    if kind == "default":
        if declaration.startswith("environment-default:"):
            return declaration.removeprefix("environment-default:") in {
                variable for _, variable, _ in ENVIRONMENT_BINDINGS
            }
        return schema is not None and declaration == f"schema.v{schema}:{path}"
    return declaration in {
        "legacy-config-schema-v1",
        "service-area-same-fips-union",
        "nwws-credential-default-detection",
    }


def _environment_manifest(value: object) -> list[dict[str, Any]]:
    items = _list(value, maximum=64)
    output = [_environment_item(raw) for raw in items]
    expected = sorted(variable for _, variable, _ in ENVIRONMENT_BINDINGS)
    if [item["variable"] for item in output] != expected:
        raise _Rejected(CONTRADICTORY)
    return output


def _environment_item(value: object) -> dict[str, Any]:
    item = _mapping(value)
    _exact_keys(item, {"variable", "present"}, {"opaque_change_identity"})
    variable = _string(item["variable"], maximum=128)
    present = _boolean(item["present"])
    opaque = _opaque_environment_identity(item.get("opaque_change_identity"))
    if not present and opaque is not None:
        raise _Rejected(CONTRADICTORY)
    normalized: dict[str, Any] = {"variable": variable, "present": present}
    if opaque is not None:
        normalized["opaque_change_identity"] = opaque
    return normalized


def _opaque_environment_identity(value: object) -> str | None:
    if value is None:
        return None
    opaque = _string(value, maximum=76)
    if not opaque.startswith("hmac-sha256:") or _sha256(opaque.removeprefix("hmac-sha256:")) is None:
        raise _Rejected()
    return opaque


def _stamp(
    value: object,
    *,
    candidate: Mapping[str, object],
    supported: SupportedCompatibility,
    stamp_version: int,
    validation_protocol_version: int,
    report_version: int,
) -> dict[str, Any]:
    stamp = _mapping(value)
    _exact_keys(
        stamp,
        {
            "stamp_version",
            "software_version",
            "build_identity",
            "validation_protocol_version",
            "supported_config_schema",
            "selected_config_schema",
            "swwp_protocol_version",
            "job_payload_schema_versions",
            "job_result_schema_versions",
            "diagnostic_schema_version",
            "diagnostic_catalog_version",
            "capability_manifest_version",
            "candidate_sha256",
            "candidate_identity_sha256",
            "active_configuration_generation",
            "started_at",
            "completed_at",
            "rule_identities",
            "probe_identities",
        },
    )
    if _integer(stamp["stamp_version"], minimum=1) != stamp_version:
        raise _Rejected(UNSUPPORTED)
    if _integer(stamp["validation_protocol_version"], minimum=1) != validation_protocol_version:
        raise _Rejected(INCOMPATIBLE)
    candidate_sha = _sha256(stamp["candidate_sha256"], nullable=True)
    candidate_identity_sha = _sha256(stamp["candidate_identity_sha256"])
    selected_schema = _integer(stamp["selected_config_schema"], minimum=1, nullable=True)
    _validate_stamp_candidate(
        stamp,
        candidate,
        supported,
        candidate_sha=candidate_sha,
        candidate_identity_sha=candidate_identity_sha,
        selected_schema=selected_schema,
    )
    _validate_stamp_times(stamp)
    rules = _identity_list(stamp["rule_identities"], maximum=128)
    probes = _identity_list(stamp["probe_identities"], maximum=_MAX_PROBES)
    identity = _stamp_identity(
        stamp,
        validation_protocol_version=validation_protocol_version,
        selected_schema=selected_schema,
        report_version=report_version,
    )
    findings = analyze_compatibility(identity, supported)
    if not all(
        finding.disposition.compatible or (finding.field == "config_schema_version" and selected_schema is None)
        for finding in findings
    ):
        raise _Rejected(INCOMPATIBLE)
    return {
        **stamp,
        "candidate_sha256": candidate_sha,
        "candidate_identity_sha256": candidate_identity_sha,
        "selected_config_schema": selected_schema,
        "active_configuration_generation": _integer(
            stamp["active_configuration_generation"],
            nullable=True,
        ),
        "rule_identities": rules,
        "probe_identities": probes,
    }


def _validate_stamp_candidate(
    stamp: Mapping[str, object],
    candidate: Mapping[str, object],
    supported: SupportedCompatibility,
    *,
    candidate_sha: str | None,
    candidate_identity_sha: str | None,
    selected_schema: int | None,
) -> None:
    if candidate_sha != candidate["sha256"]:
        raise _Rejected(CANDIDATE_MISMATCH)
    if candidate_identity_sha != candidate["identity_sha256"]:
        raise _Rejected(CANDIDATE_MISMATCH)
    if selected_schema != candidate["config_schema_version"]:
        raise _Rejected(CONTRADICTORY)
    declared_range = _mapping(stamp["supported_config_schema"])
    _exact_keys(declared_range, {"minimum", "maximum"})
    minimum = _required_version(declared_range["minimum"])
    maximum = _required_version(declared_range["maximum"])
    if (
        minimum > maximum
        or {
            "minimum": minimum,
            "maximum": maximum,
        }
        != supported.config_schema.to_dict()
    ):
        raise _Rejected(INCOMPATIBLE)


def _validate_stamp_times(stamp: Mapping[str, object]) -> None:
    started = _timestamp(stamp["started_at"])
    completed = _timestamp(stamp["completed_at"])
    if completed < started or completed - started > dt.timedelta(seconds=VALIDATION_ENVELOPE_SECONDS):
        raise _Rejected(CONTRADICTORY)


def _stamp_identity(
    stamp: Mapping[str, object],
    *,
    validation_protocol_version: int,
    selected_schema: int | None,
    report_version: int,
) -> CompatibilityIdentity:
    build = stamp["build_identity"]
    if build is not None:
        _string(build, maximum=128)
    return CompatibilityIdentity(
        software_version=_string(stamp["software_version"], maximum=64),
        build_identity=build if isinstance(build, str) else None,
        validation_protocol_version=validation_protocol_version,
        config_schema_version=selected_schema,
        swwp_protocol_version=_required_version(stamp["swwp_protocol_version"]),
        job_payload_schema_versions=_version_tuple(stamp["job_payload_schema_versions"]),
        job_result_schema_versions=_version_tuple(stamp["job_result_schema_versions"]),
        diagnostic_schema_version=_required_version(stamp["diagnostic_schema_version"]),
        diagnostic_catalog_version=_required_version(stamp["diagnostic_catalog_version"]),
        capability_manifest_version=_required_version(stamp["capability_manifest_version"]),
        report_schema_version=report_version,
    )


def _required_version(value: object) -> int:
    result = _integer(value, minimum=1)
    if result is None:
        raise _Rejected()
    return result


def _version_tuple(value: object) -> tuple[int, ...]:
    items = _list(value, maximum=32)
    output = tuple(_required_version(item) for item in items)
    if not output or list(output) != sorted(set(output)):
        raise _Rejected(CONTRADICTORY)
    return output


def _identity_list(value: object, *, maximum: int) -> tuple[str, ...]:
    output = tuple(_string(item, maximum=128) for item in _list(value, maximum=maximum))
    if list(output) != sorted(set(output)):
        raise _Rejected(CONTRADICTORY)
    return output


def _timestamp(value: object) -> dt.datetime:
    raw = _string(value, maximum=40)
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise _Rejected()
    if parsed.isoformat() != raw:
        raise _Rejected()
    return parsed


def _stages(value: object, *, candidate_sha256: str | None) -> _Observed:
    items = _list(value, maximum=len(STAGE_ORDER))
    if len(items) != len(STAGE_ORDER):
        raise _Rejected(CONTRADICTORY)
    observed = _Observed()
    for expected_stage, raw in zip(STAGE_ORDER, items, strict=True):
        stage = _stage(raw, expected_stage, candidate_sha256=candidate_sha256)
        observed.stages.append(stage)
        observed.issues.extend(stage["issues"])
        observed.probes.extend(stage["probes"])
    if len(observed.issues) > _MAX_ISSUES or len(observed.probes) > _MAX_PROBES:
        raise _Rejected()
    return observed


def _stage(
    value: object,
    expected: ValidationStage,
    *,
    candidate_sha256: str | None,
) -> dict[str, Any]:
    stage = _mapping(value)
    _exact_keys(stage, {"stage", "state", "issues"}, {"skipped_reason", "probe_results"})
    if stage["stage"] != expected.value:
        raise _Rejected(CONTRADICTORY)
    try:
        state = StageState(_string(stage["state"], maximum=16))
    except (TypeError, ValueError) as exc:
        raise _Rejected() from exc
    issues, probes = _stage_results(
        stage,
        expected,
        candidate_sha256=candidate_sha256,
    )
    reason = stage.get("skipped_reason")
    _validate_stage_state(state, reason, issues=issues, probes=probes)
    _validate_stage_probes(expected, probes)
    if issues != sorted(issues, key=_issue_sort_key):
        raise _Rejected(CONTRADICTORY)
    return {
        "stage": expected,
        "state": state,
        "issues": issues,
        "probes": probes,
        "reason": reason,
    }


def _stage_results(
    stage: Mapping[str, object],
    expected: ValidationStage,
    *,
    candidate_sha256: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    issues = [
        _issue(item, expected, candidate_sha256=candidate_sha256)
        for item in _list(stage["issues"], maximum=_MAX_ISSUES)
    ]
    probes = [_probe(item) for item in _list(stage.get("probe_results", []), maximum=_MAX_PROBES)]
    return issues, probes


def _validate_stage_state(
    state: StageState,
    reason: object,
    *,
    issues: list[dict[str, Any]],
    probes: list[dict[str, Any]],
) -> None:
    if state is StageState.SKIPPED:
        _string(reason, maximum=256)
        if issues or probes:
            raise _Rejected(CONTRADICTORY)
    elif reason is not None:
        raise _Rejected(CONTRADICTORY)


def _validate_stage_probes(
    stage: ValidationStage,
    probes: list[dict[str, Any]],
) -> None:
    if stage is not ValidationStage.PREFLIGHT and probes:
        raise _Rejected(CONTRADICTORY)


def _issue(
    value: object,
    stage: ValidationStage,
    *,
    candidate_sha256: str | None,
) -> dict[str, Any]:
    issue = _mapping(value)
    _exact_keys(
        issue,
        {
            "code",
            "rule_id",
            "diagnostic_rule_id",
            "phase",
            "severity",
            "blocking",
            "message",
            "redacted",
        },
        {
            "path",
            "primary_location",
            "related_locations",
            "notes",
            "operational_effect",
            "help",
            "documentation_reference",
            "fixes",
            "retryable",
        },
    )
    code, severity, blocking, redacted, message = _issue_identity(issue, stage)
    path, primary, related, notes = _issue_evidence(issue)
    fixes = _issue_fixes(
        issue,
        validator_rule_id=_string(issue["rule_id"], maximum=128),
        code=code,
        candidate_sha256=candidate_sha256,
        redacted=redacted,
    )
    return {
        **issue,
        "severity": severity.value,
        "blocking": blocking,
        "message": message,
        "path_parsed": path,
        "primary_parsed": primary,
        "related_parsed": related,
        "notes_parsed": notes,
        "fixes_parsed": fixes,
    }


def _issue_identity(
    issue: Mapping[str, object],
    stage: ValidationStage,
) -> tuple[str, DiagnosticSeverity, bool, bool, str]:
    validator_rule = _string(issue["rule_id"], maximum=128)
    diagnostic_rule = _string(issue["diagnostic_rule_id"], maximum=128)
    if issue["phase"] != stage.value or not validate_rule_binding(
        validator_rule,
        diagnostic_rule,
        stage,
    ):
        raise _Rejected(CONTRADICTORY)
    code = _string(issue["code"], maximum=16)
    try:
        expected_code = code_for_rule(diagnostic_rule)
    except ValueError as exc:
        raise _Rejected(CONTRADICTORY) from exc
    if code != expected_code:
        raise _Rejected(CONTRADICTORY)
    try:
        severity = DiagnosticSeverity(_string(issue["severity"], maximum=16))
    except (TypeError, ValueError) as exc:
        raise _Rejected() from exc
    blocking = _boolean(issue["blocking"])
    redacted = _boolean(issue["redacted"])
    contract = issue_contract(validator_rule, diagnostic_rule, stage)
    if (
        contract is None
        or (severity.value, blocking) not in contract.outcomes
        or (contract.redacted is not None and redacted is not contract.redacted)
    ):
        raise _Rejected(CONTRADICTORY)
    _validate_diagnostic_outcome(
        diagnostic_rule,
        severity=severity,
        blocking=blocking,
    )
    message = _string(issue["message"], maximum=512)
    return code, severity, blocking, redacted, message


def _validate_diagnostic_outcome(
    diagnostic_rule: str,
    *,
    severity: DiagnosticSeverity,
    blocking: bool,
) -> None:
    exact = {
        "compatibility.advisory": (DiagnosticSeverity.SUGGESTION, False),
        "compatibility.unsupported": (DiagnosticSeverity.ERROR, True),
        "compatibility.degraded": (DiagnosticSeverity.WARNING, False),
        "preflight.dependency_unavailable": (DiagnosticSeverity.ERROR, True),
        "preflight.degraded": (DiagnosticSeverity.WARNING, False),
    }
    expected = exact.get(diagnostic_rule)
    if expected is not None and (severity, blocking) != expected:
        raise _Rejected(CONTRADICTORY)


def _issue_evidence(
    issue: Mapping[str, object],
) -> tuple[
    DiagnosticPath | None,
    dict[str, Any] | None,
    list[dict[str, Any]],
    list[str],
]:
    path = _path(issue["path"]) if "path" in issue else None
    primary = _location(issue["primary_location"]) if "primary_location" in issue else None
    related = [_related_location(item) for item in _list(issue.get("related_locations", []), maximum=8)]
    notes = [_string(item, maximum=512) for item in _list(issue.get("notes", []), maximum=8)]
    for name in ("operational_effect", "help", "documentation_reference"):
        if name in issue:
            _string(issue[name], maximum=512)
    retryable = issue.get("retryable")
    if retryable is not None:
        _boolean(retryable)
    return path, primary, related, notes


def _issue_fixes(
    issue: Mapping[str, object],
    *,
    validator_rule_id: str,
    code: str,
    candidate_sha256: str | None,
    redacted: bool,
) -> list[dict[str, Any]]:
    fixes = [
        _fix(item, code=code, candidate_sha256=candidate_sha256) for item in _list(issue.get("fixes", []), maximum=4)
    ]
    if redacted and fixes:
        raise _Rejected(CONTRADICTORY)
    _validate_registered_fixes(
        validator_rule_id,
        fixes,
        candidate_sha256=candidate_sha256,
    )
    return fixes


def _path(value: object) -> DiagnosticPath:
    path = _mapping(value)
    _exact_keys(path, {"kind", "segments", "pointer", "human"})
    try:
        kind = PathKind(_string(path["kind"], maximum=32))
    except (TypeError, ValueError) as exc:
        raise _Rejected() from exc
    segments: list[str | int] = []
    for segment in _list(path["segments"], maximum=64):
        if isinstance(segment, bool) or not isinstance(segment, str | int):
            raise _Rejected()
        segments.append(segment)
    parsed = DiagnosticPath(kind, tuple(segments))
    if path["pointer"] != parsed.to_pointer() or path["human"] != parsed.to_human():
        raise _Rejected(CONTRADICTORY)
    return parsed


def _location(value: object) -> dict[str, Any]:
    location = _mapping(value)
    _exact_keys(location, {"source", "span", "label", "role"})
    source = _string(location["source"], maximum=512)
    label = _string(location["label"], maximum=256, allow_empty=True)
    role = _string(location["role"], maximum=64)
    span = _mapping(location["span"])
    _exact_keys(span, {"start", "end"})
    start = _position(span["start"])
    end = _position(span["end"])
    if (end["line"], end["column"]) < (start["line"], start["column"]):
        raise _Rejected(CONTRADICTORY)
    return {"source": source, "label": label, "role": role, "start": start, "end": end}


def _position(value: object) -> dict[str, int]:
    position = _mapping(value)
    _exact_keys(position, {"line", "column"}, {"offset"})
    output = {
        "line": _required_nonnegative(position["line"]),
        "column": _required_nonnegative(position["column"]),
    }
    if "offset" in position:
        output["offset"] = _required_nonnegative(position["offset"])
    return output


def _required_nonnegative(value: object) -> int:
    result = _integer(value)
    if result is None:
        raise _Rejected()
    return result


def _related_location(value: object) -> dict[str, Any]:
    related = _mapping(value)
    _exact_keys(related, {"relationship", "location"})
    return {
        "relationship": _string(related["relationship"], maximum=256),
        "location": _location(related["location"]),
    }


def _fix(value: object, *, code: str, candidate_sha256: str | None) -> dict[str, Any]:
    fix = _mapping(value)
    _exact_keys(
        fix,
        {"operation", "target", "diagnostic_code", "safety", "applicability"},
        {
            "replacement",
            "expected_old_value",
            "expected_source_sha256",
            "location",
        },
    )
    operation = _fix_operation(fix)
    _validate_fix_binding(fix, operation=operation, code=code)
    target = _path(fix["target"])
    [_string(item, maximum=160) for item in _list(fix["applicability"], maximum=8)]
    if "expected_source_sha256" in fix and _sha256(fix["expected_source_sha256"]) != candidate_sha256:
        raise _Rejected(CONTRADICTORY)
    if "location" in fix:
        _location(fix["location"])
    return {**fix, "target_parsed": target}


def _fix_operation(fix: Mapping[str, object]) -> FixOperation:
    try:
        operation = FixOperation(_string(fix["operation"], maximum=16))
        FixSafety(_string(fix["safety"], maximum=32))
    except (TypeError, ValueError) as exc:
        raise _Rejected() from exc
    return operation


def _validate_fix_binding(
    fix: Mapping[str, object],
    *,
    operation: FixOperation,
    code: str,
) -> None:
    if fix["diagnostic_code"] != code:
        raise _Rejected(CONTRADICTORY)
    replacement_present = "replacement" in fix
    if operation is FixOperation.REMOVE and replacement_present:
        raise _Rejected(CONTRADICTORY)
    if operation is not FixOperation.REMOVE and not replacement_present:
        raise _Rejected(CONTRADICTORY)


def _validate_registered_fixes(
    validator_rule_id: str,
    fixes: list[dict[str, Any]],
    *,
    candidate_sha256: str | None,
) -> None:
    expected = _registered_fix_spec(validator_rule_id)
    if expected is None:
        if fixes:
            raise _Rejected(CONTRADICTORY)
        return
    if len(fixes) != 1:
        raise _Rejected(CONTRADICTORY)
    fix = fixes[0]
    _validate_registered_fix_shape(fix, expected, candidate_sha256=candidate_sha256)
    _validate_registered_fix_values(validator_rule_id, fix)


def _registered_fix_spec(
    validator_rule_id: str,
) -> tuple[FixOperation, tuple[str, ...], tuple[str, ...]] | None:
    return {
        "semantic.lifecycle.total_covers_stage": (
            FixOperation.REPLACE,
            ("lifecycle", "total_seconds"),
            ("The stage timeout values are otherwise unchanged.",),
        ),
        "deprecation.cycle.hwo_max_chars_heightened": (
            FixOperation.REMOVE,
            ("cycle", "hwo", "max_chars_heightened"),
            ("Remove when configuration schema 1 compatibility is retired.",),
        ),
        "deprecation.cycle.afd_max_chars_heightened": (
            FixOperation.REMOVE,
            ("cycle", "afd", "max_chars_heightened"),
            ("Remove when configuration schema 1 compatibility is retired.",),
        ),
        "advisory.tts.espeak_ng_alias": (
            FixOperation.REPLACE,
            ("tts", "backend"),
            ("Remove when the underscore compatibility alias is retired.",),
        ),
    }.get(validator_rule_id)


def _validate_registered_fix_shape(
    fix: Mapping[str, Any],
    expected: tuple[FixOperation, tuple[str, ...], tuple[str, ...]],
    *,
    candidate_sha256: str | None,
) -> None:
    target = fix.get("target_parsed")
    operation, segments, applicability = expected
    if (
        fix.get("operation") != operation.value
        or fix.get("safety") != FixSafety.SAFE.value
        or not isinstance(target, DiagnosticPath)
        or target.kind is not PathKind.CONFIGURATION
        or target.segments != segments
        or tuple(fix.get("applicability", ())) != applicability
        or fix.get("expected_source_sha256") != candidate_sha256
        or "expected_old_value" not in fix
    ):
        raise _Rejected(CONTRADICTORY)


def _validate_registered_fix_values(
    validator_rule_id: str,
    fix: Mapping[str, Any],
) -> None:
    if validator_rule_id.startswith("deprecation."):
        _validate_remove_fix(fix)
        return
    if validator_rule_id == "advisory.tts.espeak_ng_alias":
        _validate_tts_alias_fix(fix)
        return
    _validate_lifecycle_fix_values(fix)


def _validate_remove_fix(fix: Mapping[str, Any]) -> None:
    if "replacement" in fix:
        raise _Rejected(CONTRADICTORY)


def _validate_tts_alias_fix(fix: Mapping[str, Any]) -> None:
    if fix.get("expected_old_value") != "espeak_ng" or fix.get("replacement") != "espeak-ng":
        raise _Rejected(CONTRADICTORY)


def _validate_lifecycle_fix_values(fix: Mapping[str, Any]) -> None:
    old = fix.get("expected_old_value")
    replacement = fix.get("replacement")
    if (
        isinstance(old, bool)
        or not isinstance(old, int | float)
        or isinstance(replacement, bool)
        or not isinstance(replacement, int | float)
        or old <= 0
        or replacement <= old
    ):
        raise _Rejected(CONTRADICTORY)


def _probe(value: object) -> dict[str, Any]:
    probe = _mapping(value)
    _exact_keys(
        probe,
        {
            "identifier",
            "owner",
            "status",
            "required",
            "fallback_available",
            "blocking",
            "redaction",
            "summary",
            "retryable",
            "elapsed_milliseconds",
            "evidence",
        },
        {"failure_kind"},
    )
    identifier, owner, status = _probe_identity(probe)
    required = _boolean(probe["required"])
    fallback = _boolean(probe["fallback_available"])
    blocking = _boolean(probe["blocking"])
    redaction = _probe_redaction(probe["redaction"])
    retryable = _boolean(probe["retryable"])
    summary = _string(probe["summary"], maximum=256)
    elapsed = _integer(probe["elapsed_milliseconds"])
    if elapsed is None or elapsed > 30_000:
        raise _Rejected()
    evidence = [_string(item, maximum=128) for item in _list(probe["evidence"], maximum=8)]
    _validate_probe_redaction(
        status,
        redaction,
        summary=summary,
        evidence=evidence,
    )
    failure = _probe_failure(probe.get("failure_kind"), status)
    _validate_probe_blocking(
        status,
        required=required,
        fallback=fallback,
        blocking=blocking,
    )
    return {
        "identifier": identifier,
        "owner": owner,
        "status": status,
        "required": required,
        "fallback_available": fallback,
        "blocking": blocking,
        "redaction": redaction,
        "summary": summary,
        "retryable": retryable,
        "elapsed_milliseconds": elapsed,
        "evidence": evidence,
        "failure_kind": failure,
    }


def _probe_identity(
    probe: Mapping[str, object],
) -> tuple[str, str, ProbeStatus]:
    identifier = _string(probe["identifier"], maximum=64)
    owner = _string(probe["owner"], maximum=64)
    try:
        status = ProbeStatus(_string(probe["status"], maximum=32))
    except (TypeError, ValueError) as exc:
        raise _Rejected() from exc
    return identifier, owner, status


def _probe_redaction(value: object) -> ProbeRedaction:
    try:
        return ProbeRedaction(_string(value, maximum=32))
    except (TypeError, ValueError) as exc:
        raise _Rejected() from exc


def _validate_probe_redaction(
    status: ProbeStatus,
    redaction: ProbeRedaction,
    *,
    summary: str,
    evidence: list[str],
) -> None:
    if summary != canonical_probe_summary(status):
        raise _Rejected(CONTRADICTORY)
    if redaction is ProbeRedaction.LOCAL_PATH_BASENAME:
        if not all(safe_probe_evidence(item) for item in evidence):
            raise _Rejected(CONTRADICTORY)
    elif evidence:
        raise _Rejected(CONTRADICTORY)


def _probe_failure(
    value: object,
    status: ProbeStatus,
) -> ProbeFailureKind | None:
    if value is None:
        return None
    try:
        failure = ProbeFailureKind(_string(value, maximum=32))
    except (TypeError, ValueError) as exc:
        raise _Rejected() from exc
    if status is not ProbeStatus.INDETERMINATE:
        raise _Rejected(CONTRADICTORY)
    return failure


def _validate_probe_blocking(
    status: ProbeStatus,
    *,
    required: bool,
    fallback: bool,
    blocking: bool,
) -> None:
    unavailable = status in {
        ProbeStatus.UNAVAILABLE,
        ProbeStatus.UNSUPPORTED,
        ProbeStatus.INDETERMINATE,
    }
    if blocking != (required and not fallback and unavailable):
        raise _Rejected(CONTRADICTORY)


def _reconcile_preflight(observed: _Observed) -> None:
    preflight_issues = [issue for issue in observed.issues if issue["phase"] == ValidationStage.PREFLIGHT.value]
    expected_issue_count = sum(_reconcile_probe(preflight_issues, probe) for probe in observed.probes)
    if len(preflight_issues) != expected_issue_count:
        raise _Rejected(CONTRADICTORY)


def _reconcile_probe(
    issues: list[dict[str, Any]],
    probe: Mapping[str, Any],
) -> int:
    status = probe["status"]
    failure = probe["failure_kind"]
    if failure is not None and (status is not ProbeStatus.INDETERMINATE or probe["retryable"] is not True):
        raise _Rejected(CONTRADICTORY)
    matches = _probe_issue_matches(issues, probe)
    if status in {ProbeStatus.AVAILABLE, ProbeStatus.SKIPPED}:
        if matches:
            raise _Rejected(CONTRADICTORY)
        return 0
    if len(matches) != 1 or not _probe_issue_is_coherent(matches[0], probe):
        raise _Rejected(CONTRADICTORY)
    return 1


def _probe_issue_is_coherent(
    issue: Mapping[str, Any],
    probe: Mapping[str, Any],
) -> bool:
    status = probe["status"]
    expected_severity = DiagnosticSeverity.ERROR.value if probe["blocking"] else DiagnosticSeverity.WARNING.value
    return (
        issue["diagnostic_rule_id"] == _expected_probe_rule(probe)
        and issue["severity"] == expected_severity
        and issue["blocking"] is probe["blocking"]
        and issue["message"] == f"Preflight {probe['identifier']}: {probe['summary']}"
        and issue.get("notes_parsed") == [f"status={status.value}", f"owner={probe['owner']}"]
        and issue.get("retryable") is probe["retryable"]
    )


def _expected_probe_rule(probe: Mapping[str, Any]) -> str:
    if probe["failure_kind"] is ProbeFailureKind.TIMEOUT:
        return "preflight.timeout"
    return "preflight.dependency_unavailable" if probe["blocking"] else "preflight.degraded"


def _probe_issue_matches(
    issues: list[dict[str, Any]],
    probe: Mapping[str, Any],
) -> list[dict[str, Any]]:
    expected_segments = ("preflight", probe["identifier"])
    return [
        issue
        for issue in issues
        if isinstance(issue.get("path_parsed"), DiagnosticPath)
        and issue["path_parsed"].kind is PathKind.JSON_POINTER
        and issue["path_parsed"].segments == expected_segments
    ]


def _issue_sort_key(issue: Mapping[str, Any]) -> tuple[object, ...]:
    primary = issue.get("primary_parsed")
    start = primary["start"] if isinstance(primary, Mapping) else {}
    path = issue.get("path_parsed")
    return (
        str(primary.get("source", "")) if isinstance(primary, Mapping) else "",
        int(start.get("line", 2**31)) if isinstance(start, Mapping) else 2**31,
        int(start.get("column", 2**31)) if isinstance(start, Mapping) else 2**31,
        path.sort_key() if isinstance(path, DiagnosticPath) else ("", ()),
        issue["rule_id"],
        issue["diagnostic_rule_id"],
        issue["message"],
    )


def _stage_dependencies(stages: Sequence[Mapping[str, Any]]) -> None:
    by_stage = {stage["stage"]: stage for stage in stages}
    parse = by_stage[ValidationStage.PARSE]
    schema = by_stage[ValidationStage.SCHEMA]
    parse_failed = _validate_parse_dependency(parse, stages)
    schema_failed = _validate_schema_dependency(
        schema,
        stages,
        parse_failed=parse_failed,
    )
    _validate_typed_dependencies(
        stages,
        parse_failed=parse_failed,
        schema_failed=schema_failed,
    )
    _validate_preflight_dependency(by_stage)


def _validate_candidate_stage_schema(candidate: Mapping[str, Any], observed: _Observed) -> None:
    parse_or_schema_failed = any(issue["blocking"] for stage in observed.stages[:2] for issue in stage["issues"])
    if candidate["config_schema_version"] is None and not parse_or_schema_failed:
        raise _Rejected(CONTRADICTORY)


def _validate_parse_dependency(
    parse: Mapping[str, Any],
    stages: Sequence[Mapping[str, Any]],
) -> bool:
    if parse["state"] is not StageState.COMPLETED:
        raise _Rejected(CONTRADICTORY)
    parse_failed = any(issue["blocking"] for issue in parse["issues"])
    later_completed = any(stage["state"] is StageState.COMPLETED for stage in stages[1:])
    if parse_failed and later_completed:
        raise _Rejected(CONTRADICTORY)
    return parse_failed


def _validate_schema_dependency(
    schema: Mapping[str, Any],
    stages: Sequence[Mapping[str, Any]],
    *,
    parse_failed: bool,
) -> bool:
    if not parse_failed and schema["state"] is not StageState.COMPLETED:
        raise _Rejected(CONTRADICTORY)
    schema_failed = any(issue["blocking"] for issue in schema["issues"])
    later_completed = any(stage["state"] is StageState.COMPLETED for stage in stages[2:])
    if schema_failed and later_completed:
        raise _Rejected(CONTRADICTORY)
    return schema_failed


def _validate_typed_dependencies(
    stages: Sequence[Mapping[str, Any]],
    *,
    parse_failed: bool,
    schema_failed: bool,
) -> None:
    typed = stages[2:6]
    incomplete = any(stage["state"] is not StageState.COMPLETED for stage in typed)
    if not parse_failed and not schema_failed and incomplete:
        raise _Rejected(CONTRADICTORY)


def _validate_preflight_dependency(
    by_stage: Mapping[ValidationStage, Mapping[str, Any]],
) -> None:
    preflight = by_stage[ValidationStage.PREFLIGHT]
    typed_blocking = any(
        issue["blocking"]
        for stage in (
            by_stage[ValidationStage.SEMANTIC],
            by_stage[ValidationStage.COMPATIBILITY],
        )
        for issue in stage["issues"]
    )
    if typed_blocking and preflight["state"] is StageState.COMPLETED:
        raise _Rejected(CONTRADICTORY)


def _stamp_stage_bindings(stamp: Mapping[str, Any], observed: _Observed) -> None:
    states = tuple((stage["stage"], stage["state"]) for stage in observed.stages)
    if stamp["rule_identities"] != expected_rule_identities(states):
        raise _Rejected(CONTRADICTORY)
    probe_ids = tuple(sorted(probe["identifier"] for probe in observed.probes))
    if len(probe_ids) != len(set(probe_ids)) or stamp["probe_identities"] != probe_ids:
        raise _Rejected(CONTRADICTORY)


def _policy(value: object) -> dict[str, bool]:
    policy = _mapping(value)
    _exact_keys(policy, {"warning_blocks", "warning_acknowledgment_required"})
    return {
        "warning_blocks": _boolean(policy["warning_blocks"]),
        "warning_acknowledgment_required": _boolean(policy["warning_acknowledgment_required"]),
    }


def _decision(observed: _Observed, policy: Mapping[str, bool]) -> dict[str, Any]:
    issues = observed.issues
    warning = any(issue["severity"] == DiagnosticSeverity.WARNING.value for issue in issues)
    valid = _valid_under_policy(issues, policy, warning=warning)
    preflight_ready = _preflight_ready(issues, observed)
    acknowledgment = warning and policy["warning_acknowledgment_required"]
    severity_counts, blocking_counts = _decision_counts(issues)
    return {
        "valid": valid,
        "preflight_ready": preflight_ready,
        "warning_acknowledgment_required": acknowledgment,
        "acceptable_for_reload_decision": valid and preflight_ready and not acknowledgment,
        "highest_severity": _highest_observed_severity(issues),
        "severity_counts": dict(sorted(severity_counts.items())),
        "blocking_counts": dict(sorted(blocking_counts.items())),
        "skipped_stages": _observed_skipped_stages(observed),
    }


def _valid_under_policy(
    issues: list[dict[str, Any]],
    policy: Mapping[str, bool],
    *,
    warning: bool,
) -> bool:
    validation_phases = {
        ValidationStage.PARSE.value,
        ValidationStage.SCHEMA.value,
        ValidationStage.SEMANTIC.value,
        ValidationStage.COMPATIBILITY.value,
    }
    blocking = any(issue["blocking"] and issue["phase"] in validation_phases for issue in issues)
    return not blocking and not (policy["warning_blocks"] and warning)


def _preflight_ready(
    issues: list[dict[str, Any]],
    observed: _Observed,
) -> bool:
    preflight = next(stage for stage in observed.stages if stage["stage"] is ValidationStage.PREFLIGHT)
    return (
        preflight["state"] is StageState.COMPLETED
        and not any(issue["blocking"] and issue["phase"] == ValidationStage.PREFLIGHT.value for issue in issues)
        and not any(probe["blocking"] for probe in observed.probes)
    )


def _decision_counts(
    issues: list[dict[str, Any]],
) -> tuple[Counter[str], Counter[str]]:
    severity = Counter(issue["severity"] for issue in issues)
    blocking = Counter("blocking" if issue["blocking"] else "nonblocking" for issue in issues)
    return severity, blocking


def _highest_observed_severity(issues: list[dict[str, Any]]) -> str | None:
    return max(
        (issue["severity"] for issue in issues),
        key=_SEVERITY_ORDER.__getitem__,
        default=None,
    )


def _observed_skipped_stages(observed: _Observed) -> list[dict[str, object]]:
    return [
        {"stage": stage["stage"].value, "reason": stage["reason"]}
        for stage in observed.stages
        if stage["state"] is StageState.SKIPPED
    ]


def _top_level(
    payload: Mapping[str, object],
    observed: _Observed,
    decision: Mapping[str, Any],
) -> None:
    for name in ("valid", "preflight_ready", "parse_valid", "schema_valid"):
        _boolean(payload[name])
    _validate_top_level_claims(payload, observed, decision)
    _validate_top_level_issues(payload, observed)


def _validate_top_level_claims(
    payload: Mapping[str, object],
    observed: _Observed,
    decision: Mapping[str, Any],
) -> None:
    parse = observed.stages[0]
    schema = observed.stages[1]
    parse_valid = parse["state"] is StageState.COMPLETED and not any(issue["blocking"] for issue in parse["issues"])
    schema_valid = schema["state"] is StageState.COMPLETED and not any(issue["blocking"] for issue in schema["issues"])
    if (
        payload["valid"] != decision["valid"]
        or payload["preflight_ready"] != decision["preflight_ready"]
        or payload["parse_valid"] != parse_valid
        or payload["schema_valid"] != schema_valid
    ):
        raise _Rejected(CONTRADICTORY)


def _validate_top_level_issues(
    payload: Mapping[str, object],
    observed: _Observed,
) -> None:
    top_issues = _list(payload["issues"], maximum=_MAX_ISSUES)
    serialized = [
        {key: value for key, value in issue.items() if not key.endswith("_parsed")} for issue in observed.issues
    ]
    if top_issues != serialized:
        raise _Rejected(CONTRADICTORY)


def _summary(value: object, decision: Mapping[str, Any]) -> None:
    summary = _mapping(value)
    _exact_keys(
        summary,
        {
            "valid",
            "preflight_ready",
            "warning_acknowledgment_required",
            "acceptable_for_reload_decision",
            "highest_severity",
            "severity_counts",
            "blocking_counts",
            "skipped_stages",
        },
    )
    for name in (
        "valid",
        "preflight_ready",
        "warning_acknowledgment_required",
        "acceptable_for_reload_decision",
    ):
        _boolean(summary[name])
    highest = summary["highest_severity"]
    if highest is not None:
        highest = _string(highest, maximum=16)
        if highest not in _SEVERITY_ORDER:
            raise _Rejected()
    normalized = {
        "valid": summary["valid"],
        "preflight_ready": summary["preflight_ready"],
        "warning_acknowledgment_required": summary["warning_acknowledgment_required"],
        "acceptable_for_reload_decision": summary["acceptable_for_reload_decision"],
        "highest_severity": highest,
        "severity_counts": _count_mapping(
            summary["severity_counts"],
            permitted=frozenset(_SEVERITY_ORDER),
        ),
        "blocking_counts": _count_mapping(
            summary["blocking_counts"],
            permitted=frozenset({"blocking", "nonblocking"}),
        ),
        "skipped_stages": _summary_skipped(summary["skipped_stages"]),
    }
    if normalized != dict(decision):
        raise _Rejected(CONTRADICTORY)


def _count_mapping(value: object, *, permitted: frozenset[str]) -> dict[str, int]:
    counts = _mapping(value)
    if not set(counts).issubset(permitted):
        raise _Rejected()
    output: dict[str, int] = {}
    for key, raw_count in counts.items():
        count = _integer(raw_count)
        if count is None:
            raise _Rejected()
        output[key] = count
    return dict(sorted(output.items()))


def _summary_skipped(value: object) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for raw in _list(value, maximum=len(STAGE_ORDER)):
        item = _mapping(raw)
        _exact_keys(item, {"stage", "reason"})
        stage = _string(item["stage"], maximum=32)
        if stage not in {candidate.value for candidate in ValidationStage}:
            raise _Rejected()
        output.append(
            {
                "stage": stage,
                "reason": _string(item["reason"], maximum=256),
            }
        )
    return output
