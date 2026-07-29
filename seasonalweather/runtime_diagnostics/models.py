"""Immutable runtime instance and mutable-occurrence value models."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from seasonalweather.diagnostics import DiagnosticCode
from seasonalweather.diagnostics.models import DiagnosticSeverity

from .redaction import redact_text

RUNTIME_DIAGNOSTIC_SCHEMA_VERSION = 1
OCCURRENCE_SCHEMA_VERSION = 1
FINGERPRINT_VERSION = 1
MAX_IDENTIFIER = 128
MAX_COMPONENT = 64
MAX_MESSAGE = 512
MAX_EFFECT = 512
MAX_ACTION = 512
MAX_CONTEXT_BYTES = 4096
MAX_COUNT = 2_147_483_647
MAX_JSON_DEPTH = 12
MAX_JSON_ITEMS = 512
MAX_JSON_STRING = 1024
MAX_RESOLUTION_EVIDENCE_BYTES = 4096
MAX_RESOLUTION_NOTES = 8
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")


class DiagnosticRole(StrEnum):
    CONTROLLER = "controller"
    WORKER = "worker"


class PromotionReason(StrEnum):
    RETRIES_EXHAUSTED = "retries_exhausted"
    PERMANENT_FAILURE = "permanent_failure"
    DEGRADATION = "degradation"
    FALLBACK_SELECTED = "fallback_selected"
    QUARANTINE = "quarantine"
    ROLLBACK = "rollback"
    INVARIANT_VIOLATION = "invariant_violation"
    PROCESS_TERMINATION = "process_termination"
    OPERATOR_ATTENTION = "operator_attention"
    RECONCILIATION = "reconciliation"


class TransitionIntent(StrEnum):
    ACTIVATE = "activate"
    RESOLVE = "resolve"


class OccurrenceState(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"


JsonScalar = str | int | float | bool | None
FrozenJson = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]


def utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(dt.UTC)


def timestamp(value: dt.datetime) -> str:
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identifier(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded opaque identifier")
    if redact_text(value, limit=MAX_IDENTIFIER) != value:
        raise ValueError(f"{name} must not contain secret material")
    return value


def freeze_json(
    value: object,
    *,
    redact_strings: bool = False,
    _depth: int = 0,
    _budget: list[int] | None = None,
) -> FrozenJson:
    if _depth > MAX_JSON_DEPTH:
        raise ValueError("structured evidence exceeds depth limit")
    budget = _budget if _budget is not None else [MAX_JSON_ITEMS]
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("structured evidence exceeds item limit")
    if isinstance(value, Mapping):
        return _freeze_mapping(
            value,
            redact_strings=redact_strings,
            depth=_depth,
            budget=budget,
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(
            freeze_json(
                item,
                redact_strings=redact_strings,
                _depth=_depth + 1,
                _budget=budget,
            )
            for item in value
        )
    return _freeze_scalar(value, redact_strings=redact_strings)


def _freeze_mapping(
    value: Mapping[object, object],
    *,
    redact_strings: bool,
    depth: int,
    budget: list[int],
) -> Mapping[str, FrozenJson]:
    frozen: dict[str, FrozenJson] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 64:
            raise ValueError("structured evidence key is invalid")
        frozen[key] = freeze_json(
            item,
            redact_strings=redact_strings,
            _depth=depth + 1,
            _budget=budget,
        )
    return MappingProxyType(frozen)


def _freeze_scalar(value: object, *, redact_strings: bool) -> JsonScalar:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return ensure_finite(value)
    if isinstance(value, str):
        text = redact_text(value, limit=MAX_JSON_STRING) if redact_strings else value
        if len(text) > MAX_JSON_STRING:
            raise ValueError("structured evidence string exceeds limit")
        return text
    raise ValueError("structured evidence contains an unsupported type")


def thaw_json(value: FrozenJson) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class CorrelationContext:
    role: DiagnosticRole
    instance_id: str
    component: str
    build_identity: str | None = None
    configuration_generation: int | None = None
    command_id: str | None = None
    job_id: str | None = None
    attempt: int | None = None
    lease_id: str | None = None
    worker_id: str | None = None
    swwp_session_id: str | None = None
    capability: str | None = None
    source_id: str | None = None
    event_id: str | None = None
    alert_id: str | None = None
    product_id: str | None = None
    segment_key: str | None = None
    replay_policy: str | None = None
    reason_code: str | None = None
    job_class: str | None = None
    deadline: dt.datetime | None = None

    def __post_init__(self) -> None:
        _identifier(self.instance_id, "instance_id")
        _validate_component(self.component)
        _validate_optional_identifiers(self)
        if self.configuration_generation is not None and not 0 <= self.configuration_generation <= MAX_COUNT:
            raise ValueError("configuration_generation is out of bounds")
        if self.attempt is not None and not 1 <= self.attempt <= 1_000:
            raise ValueError("attempt is out of bounds")
        if self.deadline is not None:
            utc(self.deadline)
        if len(self.canonical_json()) > MAX_CONTEXT_BYTES:
            raise ValueError("correlation context exceeds byte limit")

    def to_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {"role": self.role.value}
        for name in (
            "instance_id",
            "build_identity",
            "configuration_generation",
            "component",
            "command_id",
            "job_id",
            "attempt",
            "lease_id",
            "worker_id",
            "swwp_session_id",
            "capability",
            "source_id",
            "event_id",
            "alert_id",
            "product_id",
            "segment_key",
            "replay_policy",
            "reason_code",
            "job_class",
        ):
            value = getattr(self, name)
            if value is not None:
                values[name] = value
        if self.deadline is not None:
            values["deadline"] = timestamp(self.deadline)
        return values

    def canonical_json(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    def fingerprint_fields(self) -> MappingProxyType[str, Any]:
        values = {
            "component": self.component,
            "role": self.role.value,
            "capability": self.capability,
            "reason_code": self.reason_code,
            "job_class": self.job_class,
        }
        return MappingProxyType({key: value for key, value in values.items() if value is not None})


def _validate_component(component: str) -> None:
    if not component or len(component) > MAX_COMPONENT or redact_text(component, limit=MAX_COMPONENT) != component:
        raise ValueError("component is out of bounds")


def _validate_optional_identifiers(context: CorrelationContext) -> None:
    for name in (
        "build_identity",
        "command_id",
        "job_id",
        "lease_id",
        "worker_id",
        "swwp_session_id",
        "capability",
        "source_id",
        "event_id",
        "alert_id",
        "product_id",
        "segment_key",
        "replay_policy",
        "reason_code",
        "job_class",
    ):
        _identifier(getattr(context, name), name)


@dataclass(frozen=True)
class RuntimeDiagnostic:
    code: str
    diagnostic_schema_version: int
    catalog_version: int
    occurrence_schema_version: int
    severity: DiagnosticSeverity
    blocking: bool
    fatal: bool
    retryable: bool
    context: CorrelationContext
    message: str
    operational_effect: str
    recovery_action: str
    promotion_reason: PromotionReason
    transition_intent: TransitionIntent
    observed_at: dt.datetime
    exception_evidence: Mapping[str, FrozenJson] | None = None

    def __post_init__(self) -> None:
        DiagnosticCode.parse(self.code)
        _validate_runtime_versions(self)
        _validate_runtime_text(self)
        utc(self.observed_at)
        if self.exception_evidence is not None:
            frozen = freeze_json(self.exception_evidence, redact_strings=True)
            if not isinstance(frozen, Mapping):
                raise ValueError("exception evidence must be a mapping")
            object.__setattr__(self, "exception_evidence", frozen)
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        if redact_text(encoded, limit=len(encoded) + 1) != encoded:
            raise ValueError("runtime diagnostic contains unredacted secret material")
        if len(encoded.encode()) > 65_536:
            raise ValueError("runtime diagnostic exceeds byte limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "diagnostic_schema_version": self.diagnostic_schema_version,
            "catalog_version": self.catalog_version,
            "occurrence_schema_version": self.occurrence_schema_version,
            "severity": self.severity.value,
            "blocking": self.blocking,
            "fatal": self.fatal,
            "retryable": self.retryable,
            "context": self.context.to_dict(),
            "message": self.message,
            "operational_effect": self.operational_effect,
            "recovery_action": self.recovery_action,
            "promotion_reason": self.promotion_reason.value,
            "transition_intent": self.transition_intent.value,
            "observed_at": timestamp(self.observed_at),
            "exception_evidence": (thaw_json(self.exception_evidence) if self.exception_evidence is not None else None),
        }


def _validate_runtime_versions(instance: RuntimeDiagnostic) -> None:
    if (
        instance.diagnostic_schema_version != RUNTIME_DIAGNOSTIC_SCHEMA_VERSION
        or instance.occurrence_schema_version != OCCURRENCE_SCHEMA_VERSION
        or instance.catalog_version < 1
    ):
        raise ValueError("unsupported runtime diagnostic version")


def _validate_runtime_text(instance: RuntimeDiagnostic) -> None:
    for value, limit, name in (
        (instance.message, MAX_MESSAGE, "message"),
        (instance.operational_effect, MAX_EFFECT, "operational_effect"),
        (instance.recovery_action, MAX_ACTION, "recovery_action"),
    ):
        if not value or len(value) > limit or redact_text(value, limit=limit) != value:
            raise ValueError(f"{name} is unbounded or not redacted")


@dataclass(frozen=True)
class ResolutionEvidence:
    criterion: str | None = None
    worker_diagnostic_id: str | None = None
    recovery_state: str | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.criterion is not None:
            _identifier(self.criterion, "criterion")
        if self.worker_diagnostic_id is not None:
            _identifier(self.worker_diagnostic_id, "worker_diagnostic_id")
        if self.recovery_state is not None:
            _identifier(self.recovery_state, "recovery_state")
        if not isinstance(self.notes, tuple) or len(self.notes) > MAX_RESOLUTION_NOTES:
            raise ValueError("resolution notes are invalid")
        bounded_notes = tuple(redact_text(note, limit=256) for note in self.notes)
        if any(not note for note in bounded_notes):
            raise ValueError("resolution notes must be nonempty strings")
        object.__setattr__(self, "notes", bounded_notes)
        if len(self.canonical_json()) > MAX_RESOLUTION_EVIDENCE_BYTES:
            raise ValueError("resolution evidence exceeds byte limit")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> ResolutionEvidence:
        if value is None:
            return cls()
        allowed = {"criterion", "worker_diagnostic_id", "recovery_state", "notes"}
        if set(value) - allowed:
            raise ValueError("resolution evidence contains unknown fields")
        notes = value.get("notes", ())
        if not isinstance(notes, tuple | list):
            raise ValueError("resolution notes must be a bounded sequence")
        return cls(
            criterion=_optional_string(value.get("criterion"), "criterion"),
            worker_diagnostic_id=_optional_string(
                value.get("worker_diagnostic_id"),
                "worker_diagnostic_id",
            ),
            recovery_state=_optional_string(value.get("recovery_state"), "recovery_state"),
            notes=tuple(_required_string(note, "resolution note") for note in notes),
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.criterion is not None:
            result["criterion"] = self.criterion
        if self.worker_diagnostic_id is not None:
            result["worker_diagnostic_id"] = self.worker_diagnostic_id
        if self.recovery_state is not None:
            result["recovery_state"] = self.recovery_state
        if self.notes:
            result["notes"] = list(self.notes)
        return result

    def canonical_json(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, name)


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


@dataclass(frozen=True)
class OccurrenceTransition:
    transition_type: str
    observed_at: dt.datetime
    evidence: Mapping[str, FrozenJson]

    def __post_init__(self) -> None:
        _identifier(self.transition_type, "transition_type")
        utc(self.observed_at)
        frozen = freeze_json(self.evidence, redact_strings=True)
        if not isinstance(frozen, Mapping):
            raise ValueError("transition evidence must be a mapping")
        object.__setattr__(self, "evidence", frozen)


@dataclass(frozen=True)
class OccurrenceRecord:
    occurrence_id: str
    code: str
    state: OccurrenceState
    fingerprint: str
    fingerprint_key: str
    fingerprint_version: int
    diagnostic_schema_version: int
    catalog_version: int
    occurrence_schema_version: int
    first_seen: dt.datetime
    last_seen: dt.datetime
    count: int
    initial_instance: Mapping[str, FrozenJson]
    latest_instance: Mapping[str, FrozenJson]
    resolved_at: dt.datetime | None = None
    resolution_reason: str | None = None
    resolution_evidence: Mapping[str, FrozenJson] | None = None
    prior_occurrence_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("initial_instance", "latest_instance"):
            frozen = freeze_json(getattr(self, name), redact_strings=True)
            if not isinstance(frozen, Mapping):
                raise ValueError(f"{name} must be a mapping")
            object.__setattr__(self, name, frozen)
        if self.resolution_evidence is not None:
            resolution = freeze_json(self.resolution_evidence, redact_strings=True)
            if not isinstance(resolution, Mapping):
                raise ValueError("resolution evidence must be a mapping")
            object.__setattr__(self, "resolution_evidence", resolution)

    @property
    def duration_seconds(self) -> float | None:
        if self.resolved_at is None:
            return None
        return max(0.0, (self.resolved_at - self.first_seen).total_seconds())


def ensure_finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("non-finite values are prohibited")
    return value
