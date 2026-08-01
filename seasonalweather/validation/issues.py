"""Immutable compiler-grade validation issues and conservative fixes."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from seasonalweather.configuration.source import RelatedLocation, SourceLocation
from seasonalweather.diagnostics.bindings import code_for_rule
from seasonalweather.diagnostics.models import DiagnosticSeverity

from .paths import DiagnosticPath


class ValidationStage(StrEnum):
    PARSE = "parse"
    SCHEMA = "schema"
    SEMANTIC = "semantic"
    COMPATIBILITY = "compatibility"
    DEPRECATION = "deprecation"
    ADVISORY = "advisory"
    PREFLIGHT = "preflight"


STAGE_ORDER = tuple(ValidationStage)


class StageState(StrEnum):
    COMPLETED = "completed"
    SKIPPED = "skipped"


class FixOperation(StrEnum):
    REPLACE = "replace"
    REMOVE = "remove"
    INSERT = "insert"


class FixSafety(StrEnum):
    SAFE = "safe"
    REVIEW_REQUIRED = "review_required"


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_freeze_value(item) for item in value), key=repr))
    if _is_json_primitive(value):
        return value
    raise TypeError("validation values must be deterministic JSON values")


def _freeze_mapping(value: Mapping[object, object]) -> MappingProxyType[str, object]:
    return MappingProxyType(
        {str(key): _freeze_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    )


def _is_json_primitive(value: object) -> bool:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("validation numeric values must be finite")
        return True
    return value is None or isinstance(value, str | int | bool)


def _json_value(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _validate_issue_shape(issue: ValidationIssue) -> None:
    if not issue.message or len(issue.message) > 512:
        raise ValueError("validation issue message is empty or overlong")
    if len(issue.related) > 8 or len(issue.notes) > 8 or len(issue.fixes) > 4:
        raise ValueError("validation issue evidence is unbounded")


def _validate_issue_safety(issue: ValidationIssue) -> None:
    if issue.severity is DiagnosticSeverity.ERROR and not issue.blocking:
        raise ValueError("error diagnostics must block")
    if issue.redacted and issue.fixes:
        raise ValueError("secret-bearing diagnostics cannot expose machine-readable fixes")
    if any(fix.diagnostic_code != issue.code for fix in issue.fixes):
        raise ValueError("fix diagnostic code must match its issue")


def _validate_issue(issue: ValidationIssue) -> None:
    _validate_issue_shape(issue)
    _validate_issue_safety(issue)


def _optional_value(value: object, serialized: object) -> object | None:
    return serialized if value else None


def _optional_issue_fields(issue: ValidationIssue) -> dict[str, object]:
    result: dict[str, object] = {}
    optional = (
        ("path", _optional_value(issue.path, issue.path.to_dict() if issue.path else None)),
        (
            "primary_location",
            _optional_value(issue.primary, issue.primary.to_dict() if issue.primary else None),
        ),
        ("related_locations", _optional_value(issue.related, [item.to_dict() for item in issue.related])),
        ("notes", _optional_value(issue.notes, list(issue.notes))),
        ("operational_effect", issue.operational_effect),
        ("help", issue.help),
        ("documentation_reference", issue.documentation_reference),
        ("fixes", _optional_value(issue.fixes, [fix.to_dict() for fix in issue.fixes])),
    )
    result.update((key, value) for key, value in optional if value is not None)
    if issue.retryable is not None:
        result["retryable"] = issue.retryable
    return result


@dataclass(frozen=True)
class MachineFix:
    operation: FixOperation
    target: DiagnosticPath
    diagnostic_code: str
    safety: FixSafety
    replacement: object | None = None
    expected_old_value: object | None = None
    expected_source_sha256: str | None = None
    applicability: tuple[str, ...] = ()
    location: SourceLocation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "applicability", tuple(self.applicability))
        if self.operation is FixOperation.REMOVE and self.replacement is not None:
            raise ValueError("remove fixes cannot contain a replacement value")
        if self.operation in {FixOperation.REPLACE, FixOperation.INSERT} and self.replacement is None:
            raise ValueError("replace and insert fixes require a replacement value")
        if len(self.applicability) > 8 or any(not item or len(item) > 160 for item in self.applicability):
            raise ValueError("fix applicability is empty, overlong, or unbounded")
        object.__setattr__(self, "replacement", _freeze_value(self.replacement))
        object.__setattr__(self, "expected_old_value", _freeze_value(self.expected_old_value))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "operation": self.operation.value,
            "target": self.target.to_dict(),
            "diagnostic_code": self.diagnostic_code,
            "safety": self.safety.value,
            "applicability": list(self.applicability),
        }
        if self.operation is not FixOperation.REMOVE:
            result["replacement"] = _json_value(self.replacement)
        if self.expected_old_value is not None:
            result["expected_old_value"] = _json_value(self.expected_old_value)
        if self.expected_source_sha256:
            result["expected_source_sha256"] = self.expected_source_sha256
        if self.location:
            result["location"] = self.location.to_dict()
        return result


@dataclass(frozen=True)
class ValidationIssue:
    rule_id: str
    phase: ValidationStage
    severity: DiagnosticSeverity
    blocking: bool
    message: str
    validator_rule_id: str = ""
    path: DiagnosticPath | None = None
    primary: SourceLocation | None = None
    related: tuple[RelatedLocation, ...] = ()
    notes: tuple[str, ...] = ()
    operational_effect: str | None = None
    help: str | None = None
    documentation_reference: str | None = None
    fixes: tuple[MachineFix, ...] = ()
    redacted: bool = False
    retryable: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "related", tuple(self.related))
        object.__setattr__(self, "notes", tuple(self.notes))
        object.__setattr__(self, "fixes", tuple(self.fixes))
        if not self.validator_rule_id:
            object.__setattr__(self, "validator_rule_id", self.rule_id)
        if not self.validator_rule_id or len(self.validator_rule_id) > 128:
            raise ValueError("validator rule identity is empty or overlong")
        _validate_issue(self)

    @property
    def code(self) -> str:
        return code_for_rule(self.rule_id)

    def sort_key(self) -> tuple[object, ...]:
        position = self.primary.span.start if self.primary else None
        return (
            STAGE_ORDER.index(self.phase),
            self.primary.source_id if self.primary else "",
            position.line if position else 2**31,
            position.column if position else 2**31,
            self.path.sort_key() if self.path else ("", ()),
            self.validator_rule_id,
            self.rule_id,
            self.message,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "rule_id": self.validator_rule_id,
            "diagnostic_rule_id": self.rule_id,
            "phase": self.phase.value,
            "severity": self.severity.value,
            "blocking": self.blocking,
            "message": self.message,
            "redacted": self.redacted,
        }
        result.update(_optional_issue_fields(self))
        return result
