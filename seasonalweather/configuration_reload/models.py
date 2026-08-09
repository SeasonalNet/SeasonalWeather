"""Immutable contracts for controller-owned transactional configuration reload."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from seasonalweather.configuration.paths import ConfigPath

RELOAD_SCHEMA_VERSION = 1
RELOAD_POLICY_VERSION = 1
MAX_SAFE_POINT_SECONDS = 120.0
VALIDATION_REPORT_MAX_AGE_SECONDS = 300
VALIDATION_REPORT_CLOCK_SKEW_SECONDS = 5
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")


class ReloadDisposition(StrEnum):
    LIVE = "live"
    QUIESCENT = "quiescent"
    RESTART_REQUIRED = "restart_required"


_DISPOSITION_ORDER = {
    ReloadDisposition.LIVE: 0,
    ReloadDisposition.QUIESCENT: 1,
    ReloadDisposition.RESTART_REQUIRED: 2,
}


def most_restrictive(values: tuple[ReloadDisposition, ...]) -> ReloadDisposition:
    return max(values, key=lambda value: _DISPOSITION_ORDER[value], default=ReloadDisposition.LIVE)


class ChangeKind(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"
    EFFECTIVE_NOOP = "effective_noop"


class ReloadPhase(StrEnum):
    REQUESTED = "requested"
    CANDIDATE_CAPTURED = "candidate_captured"
    VALIDATION_QUEUED = "validation_queued"
    VALIDATION_RUNNING = "validation_running"
    REPORT_VERIFIED = "report_verified"
    CLASSIFIED = "classified"
    AWAITING_ACKNOWLEDGMENT = "awaiting_acknowledgment"
    RESTART_REQUIRED = "restart_required"
    PREPARING = "preparing"
    AWAITING_SAFE_POINT = "awaiting_safe_point"
    COMMITTING = "committing"
    COMMITTED = "committed"
    RETIRING = "retiring"
    COMPLETED = "completed"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    ROLLED_BACK = "rolled_back"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CANCELLED = "cancelled"


TERMINAL_PHASES = frozenset(
    {
        ReloadPhase.COMPLETED,
        ReloadPhase.REJECTED,
        ReloadPhase.DEFERRED,
        ReloadPhase.ROLLED_BACK,
        ReloadPhase.RECONCILIATION_REQUIRED,
        ReloadPhase.CANCELLED,
        ReloadPhase.RESTART_REQUIRED,
        ReloadPhase.AWAITING_ACKNOWLEDGMENT,
    }
)


class ReloadOutcome(StrEnum):
    COMMITTED = "committed"
    NOOP = "noop"
    DRY_RUN = "dry_run"
    INVALID = "invalid"
    RESTART_REQUIRED = "restart_required"
    ACKNOWLEDGMENT_REQUIRED = "acknowledgment_required"
    DEFERRED = "deferred"
    ROLLED_BACK = "rolled_back"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class WarningAcknowledgment:
    candidate_sha256: str
    candidate_identity_sha256: str
    report_sha256: str
    active_generation: int
    warning_identities: tuple[str, ...]
    actor: str
    acknowledged_at: dt.datetime
    validator_completed_at: dt.datetime
    expires_at: dt.datetime
    maximum_age_seconds: int = VALIDATION_REPORT_MAX_AGE_SECONDS
    clock_skew_seconds: int = VALIDATION_REPORT_CLOCK_SKEW_SECONDS
    schema_version: int = RELOAD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "warning_identities", tuple(self.warning_identities))
        object.__setattr__(self, "actor", _bounded_text(self.actor, 128))
        _require_acknowledgment_hashes(self)
        _require_acknowledgment_context(self)
        _require_warning_identities(self.warning_identities)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_sha256": self.candidate_sha256,
            "candidate_identity_sha256": self.candidate_identity_sha256,
            "report_sha256": self.report_sha256,
            "active_generation": self.active_generation,
            "warning_identities": list(self.warning_identities),
            "actor": self.actor,
            "acknowledged_at": self.acknowledged_at.astimezone(dt.UTC).isoformat(),
            "validator_completed_at": self.validator_completed_at.astimezone(dt.UTC).isoformat(),
            "expires_at": self.expires_at.astimezone(dt.UTC).isoformat(),
            "maximum_age_seconds": self.maximum_age_seconds,
            "clock_skew_seconds": self.clock_skew_seconds,
        }


@dataclass(frozen=True)
class CandidateBinding:
    """Safe durable identity for one already captured immutable candidate."""

    reference: str
    source_sha256: str
    byte_length: int
    source_manifest_sha256: str
    candidate_sha256: str
    candidate_identity_sha256: str

    def __post_init__(self) -> None:
        if not _REF_RE.fullmatch(self.reference):
            raise ValueError("candidate reference is malformed")
        for value in (
            self.source_sha256,
            self.source_manifest_sha256,
            self.candidate_sha256,
            self.candidate_identity_sha256,
        ):
            _require_sha256(value)
        if self.byte_length < 0:
            raise ValueError("candidate byte length cannot be negative")

    @classmethod
    def from_candidate(cls, candidate: CandidateRecord) -> CandidateBinding:
        return cls(
            reference=candidate.reference,
            source_sha256=candidate.source_sha256,
            byte_length=candidate.byte_length,
            source_manifest_sha256=canonical_sha256(_thaw(candidate.source_manifest)),
            candidate_sha256=candidate.candidate_sha256,
            candidate_identity_sha256=candidate.candidate_identity_sha256,
        )

    def matches(self, candidate: CandidateRecord) -> bool:
        return self == self.from_candidate(candidate)

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference,
            "source_sha256": self.source_sha256,
            "byte_length": self.byte_length,
            "source_manifest_sha256": self.source_manifest_sha256,
            "candidate_sha256": self.candidate_sha256,
            "candidate_identity_sha256": self.candidate_identity_sha256,
        }


@dataclass(frozen=True)
class ReloadRequest:
    actor: str
    reason: str | None = None
    dry_run: bool = False
    expected_generation: int | None = None
    safe_point_timeout_seconds: float = 30.0
    acknowledgment: WarningAcknowledgment | None = None
    candidate: CandidateBinding | None = None
    authorization_context: Mapping[str, object] = field(default_factory=dict)
    source_path: str | None = field(default=None, repr=False)
    schema_version: int = RELOAD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor", _bounded_text(self.actor, 128))
        if self.reason is not None:
            object.__setattr__(self, "reason", _bounded_text(self.reason, 256))
        object.__setattr__(self, "authorization_context", _freeze(self.authorization_context))
        if self.expected_generation is not None and self.expected_generation < 0:
            raise ValueError("expected generation cannot be negative")
        if not 0.1 <= float(self.safe_point_timeout_seconds) <= MAX_SAFE_POINT_SECONDS:
            raise ValueError("safe-point timeout is outside the approved bound")
        if self.schema_version != RELOAD_SCHEMA_VERSION:
            raise ValueError("unsupported reload request schema")

    def command_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "reason": self.reason,
            "dry_run": self.dry_run,
            "expected_generation": self.expected_generation,
            "safe_point_timeout_seconds": self.safe_point_timeout_seconds,
            "acknowledgment": self.acknowledgment.to_dict() if self.acknowledgment else None,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "auth_context": _thaw(self.authorization_context),
        }


@dataclass(frozen=True)
class CandidateRecord:
    reference: str
    source_name: str
    source_sha256: str
    candidate_sha256: str
    byte_length: int
    candidate_identity_sha256: str
    config_schema_version: int | None
    source_manifest: tuple[Mapping[str, object], ...]
    origin_manifest: tuple[Mapping[str, object], ...]
    environment_inputs: tuple[Mapping[str, object], ...]
    captured_at: dt.datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_manifest", _freeze_sequence_of_mappings(self.source_manifest))
        object.__setattr__(self, "origin_manifest", _freeze_sequence_of_mappings(self.origin_manifest))
        object.__setattr__(self, "environment_inputs", _freeze_sequence_of_mappings(self.environment_inputs))
        if not _REF_RE.fullmatch(self.reference):
            raise ValueError("candidate reference is malformed")
        _require_sha256(self.source_sha256)
        _require_sha256(self.candidate_sha256)
        _require_sha256(self.candidate_identity_sha256)
        if self.byte_length < 0 or (self.config_schema_version is not None and self.config_schema_version < 1):
            raise ValueError("candidate metadata is malformed")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("candidate timestamp must be timezone-aware")

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "candidate_sha256": self.candidate_sha256,
            "byte_length": self.byte_length,
            "candidate_identity_sha256": self.candidate_identity_sha256,
            "config_schema_version": self.config_schema_version,
            "source_manifest": [_thaw(item) for item in self.source_manifest],
            "origin_manifest": [_thaw(item) for item in self.origin_manifest],
            "environment_inputs": [_thaw(item) for item in self.environment_inputs],
            "captured_at": self.captured_at.astimezone(dt.UTC).isoformat(),
        }


@dataclass(frozen=True)
class DiffEntry:
    path: ConfigPath
    classification: ReloadDisposition
    policy_id: str
    kind: ChangeKind
    secret: bool
    old: object
    new: object
    old_origin: str | None = None
    new_origin: str | None = None
    source_location: Mapping[str, object] | None = None
    acknowledgment_required: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path.to_pointer(),
            "classification": self.classification.value,
            "policy_id": self.policy_id,
            "kind": self.kind.value,
            "secret": self.secret,
            "old": _thaw(self.old),
            "new": _thaw(self.new),
            "old_origin": self.old_origin,
            "new_origin": self.new_origin,
            "source_location": _thaw(self.source_location),
            "acknowledgment_required": self.acknowledgment_required,
        }

    def __post_init__(self) -> None:
        object.__setattr__(self, "old", _freeze(self.old))
        object.__setattr__(self, "new", _freeze(self.new))
        object.__setattr__(self, "source_location", _freeze(self.source_location))


@dataclass(frozen=True)
class ReloadDiff:
    active_generation: int
    active_identity_sha256: str
    candidate_identity_sha256: str
    report_sha256: str
    entries: tuple[DiffEntry, ...]
    source_only_change: bool = False
    policy_version: int = RELOAD_POLICY_VERSION
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        for value in (self.active_identity_sha256, self.candidate_identity_sha256, self.report_sha256):
            _require_sha256(value)
        if tuple(sorted(self.entries, key=lambda item: item.path)) != self.entries:
            raise ValueError("reload diff entries must be deterministically ordered")
        object.__setattr__(self, "digest", canonical_sha256(self.to_dict(include_digest=False)))

    @property
    def disposition(self) -> ReloadDisposition:
        return most_restrictive(tuple(item.classification for item in self.entries))

    @property
    def effective_change(self) -> bool:
        return any(item.kind is not ChangeKind.EFFECTIVE_NOOP for item in self.entries)

    def grouped_paths(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {item.value: [] for item in ReloadDisposition}
        for entry in self.entries:
            grouped[entry.classification.value].append(entry.path.to_pointer())
        return grouped

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "policy_version": self.policy_version,
            "active_generation": self.active_generation,
            "active_identity_sha256": self.active_identity_sha256,
            "candidate_identity_sha256": self.candidate_identity_sha256,
            "report_sha256": self.report_sha256,
            "source_only_change": self.source_only_change,
            "disposition": self.disposition.value,
            "entries": [item.to_dict() for item in self.entries],
        }
        if include_digest:
            result["digest"] = self.digest
        return result


@dataclass(frozen=True)
class SafePointSnapshot:
    blockers: tuple[str, ...]
    waited_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "blockers", tuple(self.blockers))

    def to_dict(self) -> dict[str, object]:
        return {"blockers": list(self.blockers), "waited_seconds": round(self.waited_seconds, 6)}


@dataclass(frozen=True)
class ActiveGeneration:
    generation: int
    configuration: Any = field(repr=False, compare=False)
    candidate_reference: str
    source_sha256: str
    candidate_identity_sha256: str
    report_sha256: str | None = None
    diff_sha256: str | None = None
    audit_reference: str | None = None
    resources: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError("active generation cannot be negative")
        _require_sha256(self.source_sha256)
        _require_sha256(self.candidate_identity_sha256)
        for value in (self.report_sha256, self.diff_sha256):
            if value is not None:
                _require_sha256(value)


@dataclass(frozen=True)
class ReloadResult:
    attempt_id: str
    audit_reference: str
    outcome: ReloadOutcome
    phase: ReloadPhase
    disposition: ReloadDisposition
    old_generation: int
    final_generation: int
    candidate_reference: str
    candidate_sha256: str
    candidate_identity_sha256: str
    report_sha256: str
    diff_sha256: str
    changed_paths: Mapping[str, Sequence[str]]
    warning_identities: tuple[str, ...] = ()
    diagnostic_codes: tuple[str, ...] = ()
    acknowledgment_challenge: Mapping[str, object] | None = None
    retirement_pending: bool = False
    cleanup_state: str = "not_required"
    message: str = ""

    def __post_init__(self) -> None:
        frozen_paths = {
            str(key): tuple(str(item) for item in value)
            for key, value in self.changed_paths.items()
        }
        object.__setattr__(self, "changed_paths", MappingProxyType(dict(sorted(frozen_paths.items()))))
        object.__setattr__(self, "warning_identities", tuple(self.warning_identities))
        object.__setattr__(self, "diagnostic_codes", tuple(self.diagnostic_codes))
        object.__setattr__(self, "acknowledgment_challenge", _freeze(self.acknowledgment_challenge))

    def command_result(self) -> dict[str, object]:
        result: dict[str, object] = {
            "outcome": self.outcome.value,
            "phase": self.phase.value,
            "disposition": self.disposition.value,
            "old_generation": self.old_generation,
            "final_generation": self.final_generation,
            "retirement_pending": self.retirement_pending,
            "cleanup_state": self.cleanup_state,
            "diagnostic_codes": list(self.diagnostic_codes),
            "attempt_id": self.attempt_id,
            "audit_reference": self.audit_reference,
            "candidate_reference": self.candidate_reference,
        }
        if self.acknowledgment_challenge is not None:
            result["acknowledgment_challenge"] = _thaw(self.acknowledgment_challenge)
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RELOAD_SCHEMA_VERSION,
            "attempt_id": self.attempt_id,
            "audit_reference": self.audit_reference,
            "outcome": self.outcome.value,
            "phase": self.phase.value,
            "disposition": self.disposition.value,
            "old_generation": self.old_generation,
            "final_generation": self.final_generation,
            "candidate_reference": self.candidate_reference,
            "candidate_sha256": self.candidate_sha256,
            "candidate_identity_sha256": self.candidate_identity_sha256,
            "report_sha256": self.report_sha256,
            "diff_sha256": self.diff_sha256,
            "changed_paths": {key: list(value) for key, value in self.changed_paths.items()},
            "warning_identities": list(self.warning_identities),
            "diagnostic_codes": list(self.diagnostic_codes),
            "acknowledgment_challenge": _thaw(self.acknowledgment_challenge),
            "retirement_pending": self.retirement_pending,
            "cleanup_state": self.cleanup_state,
            "message": _bounded_text(self.message, 512),
        }


def warning_identity(issue: Any) -> str:
    path = issue.path.to_dict().get("value", "root") if issue.path is not None else "root"
    raw = f"{issue.rule_id}|{issue.code}|{path}"
    return f"warning:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def canonical_sha256(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _freeze_sequence_of_mappings(
    values: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    return tuple(cast(Mapping[str, object], _freeze(item)) for item in values)


def _require_acknowledgment_hashes(value: WarningAcknowledgment) -> None:
    for digest in (value.candidate_sha256, value.candidate_identity_sha256, value.report_sha256):
        _require_sha256(digest)


def _require_acknowledgment_context(value: WarningAcknowledgment) -> None:
    if value.active_generation < 0:
        raise ValueError("acknowledgment generation cannot be negative")
    if value.schema_version != RELOAD_SCHEMA_VERSION:
        raise ValueError("unsupported acknowledgment schema")
    for timestamp in (value.acknowledged_at, value.validator_completed_at, value.expires_at):
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("acknowledgment timestamps must be timezone-aware")
    if value.maximum_age_seconds != VALIDATION_REPORT_MAX_AGE_SECONDS:
        raise ValueError("acknowledgment report-age policy is unsupported")
    if value.clock_skew_seconds != VALIDATION_REPORT_CLOCK_SKEW_SECONDS:
        raise ValueError("acknowledgment clock-skew policy is unsupported")


def _require_warning_identities(values: tuple[str, ...]) -> None:
    ordered = tuple(sorted(set(values)))
    if not ordered or ordered != values or len(ordered) > 128:
        raise ValueError("acknowledgment warnings must be a nonempty exact bounded set")
    if any(item == "*" or not _REF_RE.fullmatch(item) for item in ordered):
        raise ValueError("acknowledgment warning identity is malformed")


def _require_sha256(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("expected a lowercase SHA-256 digest")
    return value


def _bounded_text(value: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(not char.isprintable() for char in text):
        raise ValueError("text is empty, overlong, or contains unsupported characters")
    lowered = text.lower()
    if any(
        term in lowered for term in ("authorization:", "bearer ", "seasonalclient ", "password", "api_key", "api token")
    ):
        raise ValueError("text contains prohibited credential material")
    return text
