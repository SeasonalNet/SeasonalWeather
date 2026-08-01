"""One deterministic staged configuration-validation pipeline."""

from __future__ import annotations

import datetime as dt
import json
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from seasonalweather import __version__
from seasonalweather.capabilities.manifest import MANIFEST_SCHEMA_VERSION
from seasonalweather.capabilities.qualification import WorkerQualificationView
from seasonalweather.configuration.compiler import CompiledConfiguration, compile_path
from seasonalweather.configuration.issues import IssuePhase
from seasonalweather.configuration.origins import ENVIRONMENT_BINDINGS, OriginKind
from seasonalweather.configuration.schema import SUPPORTED_CONFIG_SCHEMAS
from seasonalweather.diagnostics.bindings import code_for_rule
from seasonalweather.diagnostics.models import (
    DIAGNOSTIC_CATALOG_VERSION,
    DIAGNOSTIC_SCHEMA_VERSION,
    DiagnosticSeverity,
)
from seasonalweather.jobs.registry import JOB_TYPE_POLICIES
from seasonalweather.swwp.constants import PROTOCOL_VERSION

from .advisories import evaluate_advisories
from .candidate_identity import complete_candidate_sha256, source_manifest_sha256
from .capability import CapabilityAnalysis, CapabilityNeed, analyze_capabilities
from .compatibility import (
    CompatibilityDisposition,
    CompatibilityIdentity,
    IntegerRange,
    SupportedCompatibility,
    analyze_compatibility,
)
from .issues import (
    STAGE_ORDER,
    StageState,
    ValidationIssue,
    ValidationStage,
)
from .limits import VALIDATION_ENVELOPE_SECONDS
from .paths import DiagnosticPath
from .preflight import (
    PreflightProbe,
    PreflightResult,
    ProbeExecutor,
    ProbeFailureKind,
    ProbeStatus,
    run_preflight,
)
from .rules import expected_rule_identities
from .semantic import validate_semantics

VALIDATION_PROTOCOL_VERSION = 1
VALIDATION_REPORT_VERSION = 1
VALIDATOR_STAMP_VERSION = 1
_VALIDATION_PHASES = frozenset(
    {
        ValidationStage.PARSE,
        ValidationStage.SCHEMA,
        ValidationStage.SEMANTIC,
        ValidationStage.COMPATIBILITY,
    }
)
_SEVERITY_ORDER = {
    DiagnosticSeverity.INFO: 0,
    DiagnosticSeverity.SUGGESTION: 1,
    DiagnosticSeverity.DEPRECATION: 2,
    DiagnosticSeverity.WARNING: 3,
    DiagnosticSeverity.ERROR: 4,
}


@dataclass(frozen=True)
class EnvironmentInputIdentity:
    variable: str
    present: bool
    opaque_change_identity: str | None = None

    def __post_init__(self) -> None:
        _validate_environment_input(self)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"variable": self.variable, "present": self.present}
        if self.opaque_change_identity:
            result["opaque_change_identity"] = self.opaque_change_identity
        return result


def _validate_environment_input(identity: EnvironmentInputIdentity) -> None:
    if not identity.variable or len(identity.variable) > 128:
        raise ValueError("environment identity name is empty or overlong")
    if type(identity.present) is not bool:
        raise TypeError("environment presence must be boolean")
    _validate_opaque_environment_identity(identity.opaque_change_identity)
    if not identity.present and identity.opaque_change_identity is not None:
        raise ValueError("absent environment input cannot have a change identity")


def _validate_opaque_environment_identity(value: str | None) -> None:
    if value is None:
        return
    prefix = "hmac-sha256:"
    digest = value.removeprefix(prefix)
    if (
        not value.startswith(prefix)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("opaque environment identity must be a redacted HMAC-SHA-256")


@dataclass(frozen=True)
class CandidateIdentity:
    sha256: str | None
    config_schema_version: int | None
    source_manifest: tuple[SourceInputIdentity, ...]
    origin_manifest: tuple[OriginInputIdentity, ...] = ()
    environment_inputs: tuple[EnvironmentInputIdentity, ...] = ()
    reproducible: bool = True
    identity_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_manifest", tuple(self.source_manifest))
        object.__setattr__(self, "origin_manifest", tuple(self.origin_manifest))
        object.__setattr__(self, "environment_inputs", tuple(self.environment_inputs))
        if type(self.reproducible) is not bool:
            raise TypeError("candidate reproducibility must be boolean")
        if self.config_schema_version is not None and (
            isinstance(self.config_schema_version, bool)
            or not isinstance(self.config_schema_version, int)
            or self.config_schema_version < 1
        ):
            raise ValueError("candidate configuration schema is malformed")
        _validate_candidate_identity(self)
        object.__setattr__(
            self,
            "identity_sha256",
            complete_candidate_sha256(
                source_manifest=tuple(item.to_dict() for item in self.source_manifest),
                config_schema_version=self.config_schema_version,
                origin_manifest=tuple(item.to_dict() for item in self.origin_manifest),
                environment_inputs=tuple(item.to_dict() for item in self.environment_inputs),
            ),
        )

    @classmethod
    def from_compiled(
        cls,
        compiled: CompiledConfiguration,
        *,
        environment_inputs: tuple[EnvironmentInputIdentity, ...] = (),
    ) -> CandidateIdentity:
        sources = _compiled_source_identities(compiled)
        digest = _manifest_digest(sources)
        origins = _compiled_origin_identities(compiled)
        ordered_environment = _merge_environment_identities(compiled, environment_inputs)
        reproducible = (
            digest is not None
            and all(item.bytes_available for item in sources)
            and all(not item.present or item.opaque_change_identity is not None for item in ordered_environment)
        )
        return cls(
            sha256=digest,
            config_schema_version=compiled.report.resolved_config_schema,
            source_manifest=sources,
            origin_manifest=origins,
            environment_inputs=ordered_environment,
            reproducible=reproducible,
        )

    @classmethod
    def from_source_bundle(
        cls,
        sources: tuple[tuple[str, bytes | None], ...],
        *,
        config_schema_version: int | None,
    ) -> CandidateIdentity:
        ordered = tuple(sorted(sources, key=lambda item: item[0]))
        if len({name for name, _ in ordered}) != len(ordered):
            raise ValueError("candidate source identifiers must be unique")
        if not ordered:
            return cls(
                sha256=None,
                config_schema_version=config_schema_version,
                source_manifest=(),
                reproducible=False,
            )
        manifest = tuple(SourceInputIdentity.from_bytes(name, data) for name, data in ordered)
        digest = _manifest_digest(manifest)
        return cls(digest, config_schema_version, manifest, reproducible=digest is not None)

    def to_dict(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "identity_sha256": self.identity_sha256,
            "config_schema_version": self.config_schema_version,
            "source_manifest": [item.to_dict() for item in self.source_manifest],
            "origin_manifest": [item.to_dict() for item in self.origin_manifest],
            "environment_inputs": [item.to_dict() for item in self.environment_inputs],
            "reproducible": self.reproducible,
        }


@dataclass(frozen=True, order=True)
class SourceInputIdentity:
    source: str
    sha256: str | None
    byte_length: int | None
    bytes_available: bool

    def __post_init__(self) -> None:
        _validate_source_name(self.source)
        _validate_source_digest_and_length(self.sha256, self.byte_length)
        if type(self.bytes_available) is not bool:
            raise TypeError("source byte availability must be boolean")
        if self.bytes_available != (self.sha256 is not None and self.byte_length is not None):
            raise ValueError("source byte availability contradicts its digest")

    @classmethod
    def from_bytes(cls, source: str, data: bytes | None) -> SourceInputIdentity:
        if data is None:
            return cls(source, None, None, bytes_available=False)
        import hashlib

        return cls(source, hashlib.sha256(data).hexdigest(), len(data), bytes_available=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "bytes_available": self.bytes_available,
        }


def _validate_source_name(source: str) -> None:
    if not source or len(source) > 512 or "\x00" in source:
        raise ValueError("candidate source identifier is unsafe")


def _validate_source_digest_and_length(digest: str | None, byte_length: int | None) -> None:
    if digest is not None and not _valid_sha256(digest):
        raise ValueError("candidate source SHA-256 is malformed")
    if byte_length is not None and (
        isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length < 0
    ):
        raise ValueError("candidate source byte length is malformed")


@dataclass(frozen=True, order=True)
class OriginInputIdentity:
    path: str
    kind: str
    declaration_id: str

    def __post_init__(self) -> None:
        if len(self.path) > 512 or (self.path and not self.path.startswith("/")):
            raise ValueError("candidate origin path is malformed")
        if self.kind not in {OriginKind.DEFAULT.value, OriginKind.GENERATED.value}:
            raise ValueError("candidate origin kind is unsupported")
        if not self.declaration_id or len(self.declaration_id) > 256:
            raise ValueError("candidate origin declaration is empty or overlong")

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "kind": self.kind,
            "declaration_id": self.declaration_id,
        }


def _manifest_digest(sources: tuple[SourceInputIdentity, ...]) -> str | None:
    return source_manifest_sha256(tuple(item.to_dict() for item in sources))


def _compiled_environment_inputs(
    compiled: CompiledConfiguration,
) -> tuple[EnvironmentInputIdentity, ...]:
    present = {
        origin.environment_variable
        for origin in compiled.report.origins
        if origin.kind is OriginKind.ENVIRONMENT and origin.environment_variable
    }
    return tuple(EnvironmentInputIdentity(variable, variable in present) for _, variable, _ in ENVIRONMENT_BINDINGS)


def _validate_candidate_identity(candidate: CandidateIdentity) -> None:
    if candidate.sha256 is not None and not _valid_sha256(candidate.sha256):
        raise ValueError("candidate SHA-256 is malformed")
    _validate_source_manifest(candidate.source_manifest)
    _validate_origin_manifest(candidate.origin_manifest)
    _validate_environment_manifest(candidate.environment_inputs)
    if candidate.sha256 != _manifest_digest(candidate.source_manifest):
        raise ValueError("candidate source-manifest SHA-256 is contradictory")
    if candidate.reproducible != _candidate_is_reproducible(candidate):
        raise ValueError("candidate reproducibility contradicts its bounded identities")


def _validate_source_manifest(manifest: tuple[SourceInputIdentity, ...]) -> None:
    if tuple(sorted(manifest, key=lambda item: item.source)) != manifest:
        raise ValueError("candidate source manifest must be sorted")
    if len({item.source for item in manifest}) != len(manifest):
        raise ValueError("candidate source identifiers must be unique")


def _validate_origin_manifest(manifest: tuple[OriginInputIdentity, ...]) -> None:
    if tuple(sorted(manifest, key=lambda item: (item.path, item.kind))) != manifest:
        raise ValueError("candidate origin manifest must be sorted")
    if len({(item.path, item.kind) for item in manifest}) != len(manifest):
        raise ValueError("candidate origin declarations must be unique")


def _validate_environment_manifest(manifest: tuple[EnvironmentInputIdentity, ...]) -> None:
    if tuple(sorted(manifest, key=lambda item: item.variable)) != manifest:
        raise ValueError("environment identities must be sorted")
    if len({item.variable for item in manifest}) != len(manifest):
        raise ValueError("environment identities must be unique")


def _candidate_is_reproducible(candidate: CandidateIdentity) -> bool:
    source_ready = bool(candidate.source_manifest) and all(
        item.bytes_available and item.sha256 is not None and item.byte_length is not None
        for item in candidate.source_manifest
    )
    environment_ready = all(
        not item.present or item.opaque_change_identity is not None for item in candidate.environment_inputs
    )
    return candidate.sha256 is not None and source_ready and environment_ready


def _compiled_source_identities(
    compiled: CompiledConfiguration,
) -> tuple[SourceInputIdentity, ...]:
    identities = (
        SourceInputIdentity(
            item.source_id,
            item.sha256,
            item.byte_length,
            bytes_available=item.sha256 is not None and item.byte_length is not None,
        )
        for item in compiled.report.sources
    )
    return tuple(sorted(identities, key=lambda item: item.source))


def _compiled_origin_identities(
    compiled: CompiledConfiguration,
) -> tuple[OriginInputIdentity, ...]:
    identities = (
        OriginInputIdentity(
            origin.path.to_pointer(),
            origin.kind.value,
            origin.declaration_id or "",
        )
        for origin in compiled.report.origins
        if origin.kind in {OriginKind.DEFAULT, OriginKind.GENERATED}
    )
    return tuple(sorted(identities, key=lambda item: (item.path, item.kind)))


def _merge_environment_identities(
    compiled: CompiledConfiguration,
    supplied: tuple[EnvironmentInputIdentity, ...],
) -> tuple[EnvironmentInputIdentity, ...]:
    identities = {item.variable: item for item in _compiled_environment_inputs(compiled)}
    for item in supplied:
        expected = identities.get(item.variable)
        if expected is None or (compiled.report.parse_valid and expected.present is not item.present):
            raise ValueError("environment identity contradicts compiler provenance")
        identities[item.variable] = item
    return tuple(sorted(identities.values(), key=lambda item: item.variable))


@dataclass(frozen=True)
class ValidationPolicy:
    warning_blocks: bool = False
    warning_acknowledgment_required: bool = False

    def __post_init__(self) -> None:
        if type(self.warning_blocks) is not bool or type(self.warning_acknowledgment_required) is not bool:
            raise TypeError("validation policy fields must be booleans")


@dataclass(frozen=True)
class ValidationContext:
    active_configuration_generation: int | None = None
    build_identity: str | None = None
    compatibility_identity: CompatibilityIdentity | None = None
    supported_compatibility: SupportedCompatibility | None = None
    capability_views: tuple[WorkerQualificationView, ...] = ()
    capability_needs: tuple[CapabilityNeed, ...] = ()
    preflight_enabled: bool = False
    preflight_probes: tuple[PreflightProbe, ...] = ()
    preflight_executor: ProbeExecutor | None = field(default=None, repr=False, compare=False)
    environment_inputs: tuple[EnvironmentInputIdentity, ...] = ()
    policy: ValidationPolicy = field(default_factory=ValidationPolicy)
    clock: Callable[[], dt.datetime] = field(
        default=lambda: dt.datetime.now(dt.UTC).replace(microsecond=0),
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.active_configuration_generation is not None and self.active_configuration_generation < 0:
            raise ValueError("active configuration generation cannot be negative")
        if self.build_identity is not None and (not self.build_identity or len(self.build_identity) > 128):
            raise ValueError("build identity is empty or overlong")
        object.__setattr__(
            self,
            "capability_views",
            tuple(_freeze_capability_view(item) for item in self.capability_views),
        )
        object.__setattr__(self, "capability_needs", tuple(self.capability_needs))
        object.__setattr__(self, "preflight_probes", tuple(self.preflight_probes))
        object.__setattr__(
            self,
            "environment_inputs",
            tuple(sorted(self.environment_inputs, key=lambda item: item.variable)),
        )


def _freeze_capability_view(view: WorkerQualificationView) -> WorkerQualificationView:
    records = tuple(
        record.model_copy(
            update={
                "parameters": MappingProxyType(dict(record.parameters)),
                "dependency_health": MappingProxyType(dict(record.dependency_health)),
            },
            deep=True,
        )
        for record in view.records
    )
    return WorkerQualificationView(
        worker_id=view.worker_id,
        worker_instance_id=view.worker_instance_id,
        session_id=view.session_id,
        epoch=view.epoch,
        digest=view.digest,
        records=records,
        authorized_capabilities=frozenset(view.authorized_capabilities),
        authorized_job_types=frozenset(view.authorized_job_types),
        payload_versions=cast(dict[Any, int], MappingProxyType(dict(view.payload_versions))),
        result_versions=cast(dict[Any, int], MappingProxyType(dict(view.result_versions))),
        effective_capacity=cast(dict[str, int], MappingProxyType(dict(view.effective_capacity))),
        trusted=view.trusted,
        connected=view.connected,
        probe_required=view.probe_required,
    )


@dataclass(frozen=True)
class StageResult:
    stage: ValidationStage
    state: StageState
    issues: tuple[ValidationIssue, ...] = ()
    skipped_reason: str | None = None
    preflight_results: tuple[PreflightResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "preflight_results", tuple(self.preflight_results))
        _validate_stage_result(self)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "stage": self.stage.value,
            "state": self.state.value,
            "issues": [issue.to_dict() for issue in self.issues],
        }
        if self.skipped_reason:
            result["skipped_reason"] = self.skipped_reason
        if self.preflight_results:
            result["probe_results"] = [item.to_dict() for item in self.preflight_results]
        return result


def _validate_stage_result(result: StageResult) -> None:
    _validate_stage_state(result)
    if any(issue.phase is not result.stage for issue in result.issues):
        raise ValueError("stage contains an issue from another phase")
    if result.issues != tuple(sorted(result.issues, key=ValidationIssue.sort_key)):
        raise ValueError("stage issues must be deterministically ordered")
    if result.stage is not ValidationStage.PREFLIGHT and result.preflight_results:
        raise ValueError("only preflight may contain probe results")


def _validate_stage_state(result: StageResult) -> None:
    if result.state is StageState.SKIPPED:
        _validate_skipped_stage(result)
    elif result.skipped_reason is not None:
        raise ValueError("completed stage cannot contain a skipped reason")


def _validate_skipped_stage(result: StageResult) -> None:
    if not result.skipped_reason:
        raise ValueError("skipped stage requires a reason")
    if result.issues or result.preflight_results:
        raise ValueError("skipped stages cannot contain results")


@dataclass(frozen=True)
class PolicyDecision:
    valid: bool
    preflight_ready: bool
    warning_acknowledgment_required: bool
    acceptable_for_reload_decision: bool
    highest_severity: DiagnosticSeverity | None
    severity_counts: tuple[tuple[str, int], ...]
    blocking_counts: tuple[tuple[str, int], ...]
    skipped_stages: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "severity_counts", tuple(tuple(item) for item in self.severity_counts))
        object.__setattr__(self, "blocking_counts", tuple(tuple(item) for item in self.blocking_counts))
        object.__setattr__(self, "skipped_stages", tuple(tuple(item) for item in self.skipped_stages))

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "preflight_ready": self.preflight_ready,
            "warning_acknowledgment_required": self.warning_acknowledgment_required,
            "acceptable_for_reload_decision": self.acceptable_for_reload_decision,
            "highest_severity": self.highest_severity.value if self.highest_severity else None,
            "severity_counts": dict(self.severity_counts),
            "blocking_counts": dict(self.blocking_counts),
            "skipped_stages": [{"stage": stage, "reason": reason} for stage, reason in self.skipped_stages],
        }


@dataclass(frozen=True)
class ValidatorStamp:
    stamp_version: int
    software_version: str
    build_identity: str | None
    validation_protocol_version: int
    supported_config_schema: IntegerRange
    selected_config_schema: int | None
    swwp_protocol_version: int
    job_payload_schema_versions: tuple[int, ...]
    job_result_schema_versions: tuple[int, ...]
    diagnostic_schema_version: int
    diagnostic_catalog_version: int
    capability_manifest_version: int
    candidate_sha256: str | None
    candidate_identity_sha256: str
    active_configuration_generation: int | None
    started_at: dt.datetime
    completed_at: dt.datetime
    rule_identities: tuple[str, ...]
    probe_identities: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_payload_schema_versions", tuple(self.job_payload_schema_versions))
        object.__setattr__(self, "job_result_schema_versions", tuple(self.job_result_schema_versions))
        object.__setattr__(self, "rule_identities", tuple(self.rule_identities))
        object.__setattr__(self, "probe_identities", tuple(self.probe_identities))
        if self.stamp_version != VALIDATOR_STAMP_VERSION:
            raise ValueError("unsupported validator stamp version")
        for value in (self.started_at, self.completed_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("validator stamp times must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("validator completion precedes start")
        if self.completed_at - self.started_at > dt.timedelta(seconds=VALIDATION_ENVELOPE_SECONDS):
            raise ValueError("validator duration exceeds the validation envelope")
        if self.rule_identities != tuple(sorted(set(self.rule_identities))):
            raise ValueError("validator rule identities must be unique and sorted")
        if self.probe_identities != tuple(sorted(set(self.probe_identities))):
            raise ValueError("validator probe identities must be unique and sorted")

    def to_dict(self) -> dict[str, object]:
        return {
            "stamp_version": self.stamp_version,
            "software_version": self.software_version,
            "build_identity": self.build_identity,
            "validation_protocol_version": self.validation_protocol_version,
            "supported_config_schema": self.supported_config_schema.to_dict(),
            "selected_config_schema": self.selected_config_schema,
            "swwp_protocol_version": self.swwp_protocol_version,
            "job_payload_schema_versions": list(self.job_payload_schema_versions),
            "job_result_schema_versions": list(self.job_result_schema_versions),
            "diagnostic_schema_version": self.diagnostic_schema_version,
            "diagnostic_catalog_version": self.diagnostic_catalog_version,
            "capability_manifest_version": self.capability_manifest_version,
            "candidate_sha256": self.candidate_sha256,
            "candidate_identity_sha256": self.candidate_identity_sha256,
            "active_configuration_generation": self.active_configuration_generation,
            "started_at": self.started_at.astimezone(dt.UTC).isoformat(),
            "completed_at": self.completed_at.astimezone(dt.UTC).isoformat(),
            "rule_identities": list(self.rule_identities),
            "probe_identities": list(self.probe_identities),
        }


@dataclass(frozen=True)
class ValidationReport:
    report_version: int
    candidate: CandidateIdentity
    stamp: ValidatorStamp
    stages: tuple[StageResult, ...]
    policy: ValidationPolicy
    decision: PolicyDecision

    def __post_init__(self) -> None:
        object.__setattr__(self, "stages", tuple(self.stages))
        if self.report_version != VALIDATION_REPORT_VERSION:
            raise ValueError("unsupported validation report version")
        if tuple(stage.stage for stage in self.stages) != STAGE_ORDER:
            raise ValueError("validation report stage order is invalid")
        if self.stamp.candidate_sha256 != self.candidate.sha256:
            raise ValueError("validator stamp candidate hash is contradictory")
        if self.stamp.candidate_identity_sha256 != self.candidate.identity_sha256:
            raise ValueError("validator stamp complete candidate identity is contradictory")

    @property
    def issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for stage in self.stages for issue in stage.issues)

    def stage_valid(self, selected: ValidationStage) -> bool:
        stage = next(item for item in self.stages if item.stage is selected)
        return stage.state is StageState.COMPLETED and not any(issue.blocking for issue in stage.issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "validation_report_version": self.report_version,
            "valid": self.decision.valid,
            "preflight_ready": self.decision.preflight_ready,
            "parse_valid": self.stage_valid(ValidationStage.PARSE),
            "schema_valid": self.stage_valid(ValidationStage.SCHEMA),
            "issues": [issue.to_dict() for issue in self.issues],
            "candidate": self.candidate.to_dict(),
            "validator_stamp": self.stamp.to_dict(),
            "stages": [stage.to_dict() for stage in self.stages],
            "policy": {
                "warning_blocks": self.policy.warning_blocks,
                "warning_acknowledgment_required": self.policy.warning_acknowledgment_required,
            },
            "summary": self.decision.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class VerificationFailure(StrEnum):
    MALFORMED_REPORT = "malformed_report"
    UNSUPPORTED_REPORT = "unsupported_report"
    CANDIDATE_MISMATCH = "candidate_hash_mismatch"
    STALE_GENERATION = "stale_active_generation"
    INCOMPATIBLE_STAMP = "incompatible_validator_stamp"
    CONTRADICTORY_REPORT = "contradictory_report"
    REPORT_MISMATCH = "report_binding_mismatch"


@dataclass(frozen=True)
class ReportVerification:
    accepted: bool
    failures: tuple[VerificationFailure, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", tuple(self.failures))
        if type(self.accepted) is not bool or self.accepted != (not self.failures):
            raise ValueError("report verification acceptance contradicts its failures")

    @property
    def diagnostic_code(self) -> str | None:
        return None if self.accepted else code_for_rule("validation.report_rejected")


def default_supported_compatibility() -> SupportedCompatibility:
    payload_versions = frozenset(policy.payload_schema_version for policy in JOB_TYPE_POLICIES.values())
    result_versions = frozenset(policy.result_schema_version for policy in JOB_TYPE_POLICIES.values())
    return SupportedCompatibility(
        software_minimum="0.17.0",
        software_maximum_exclusive="0.19.0",
        validation_protocol=IntegerRange(1, VALIDATION_PROTOCOL_VERSION),
        config_schema=IntegerRange(min(SUPPORTED_CONFIG_SCHEMAS), max(SUPPORTED_CONFIG_SCHEMAS)),
        swwp_protocol=IntegerRange(1, PROTOCOL_VERSION),
        job_payload_schemas=payload_versions,
        job_result_schemas=result_versions,
        diagnostic_schema=IntegerRange(1, DIAGNOSTIC_SCHEMA_VERSION),
        diagnostic_catalog=IntegerRange(1, DIAGNOSTIC_CATALOG_VERSION),
        capability_manifest=IntegerRange(1, MANIFEST_SCHEMA_VERSION),
        report_schema=IntegerRange(1, VALIDATION_REPORT_VERSION),
    )


def current_compatibility_identity(config_schema_version: int | None) -> CompatibilityIdentity:
    payload_versions = tuple(sorted({policy.payload_schema_version for policy in JOB_TYPE_POLICIES.values()}))
    result_versions = tuple(sorted({policy.result_schema_version for policy in JOB_TYPE_POLICIES.values()}))
    return CompatibilityIdentity(
        software_version=__version__,
        build_identity=None,
        validation_protocol_version=VALIDATION_PROTOCOL_VERSION,
        config_schema_version=config_schema_version,
        swwp_protocol_version=PROTOCOL_VERSION,
        job_payload_schema_versions=payload_versions,
        job_result_schema_versions=result_versions,
        diagnostic_schema_version=DIAGNOSTIC_SCHEMA_VERSION,
        diagnostic_catalog_version=DIAGNOSTIC_CATALOG_VERSION,
        capability_manifest_version=MANIFEST_SCHEMA_VERSION,
        report_schema_version=VALIDATION_REPORT_VERSION,
    )


def _compiler_issues(
    compiled: CompiledConfiguration,
    phase: ValidationStage,
) -> tuple[ValidationIssue, ...]:
    expected = IssuePhase.PARSE if phase is ValidationStage.PARSE else IssuePhase.SCHEMA
    output = (
        ValidationIssue(
            rule_id=issue.rule_id,
            validator_rule_id=f"compiler.{phase.value}",
            phase=phase,
            severity=DiagnosticSeverity(issue.severity),
            blocking=issue.blocking,
            message=issue.message,
            path=DiagnosticPath.configuration(issue.path) if issue.path else None,
            primary=issue.primary,
            related=issue.related,
            notes=issue.notes,
            help=issue.help,
            redacted=issue.redacted,
            documentation_reference="docs/configuration-compiler.md",
        )
        for issue in compiled.report.issues
        if issue.phase is expected
    )
    return tuple(sorted(output, key=ValidationIssue.sort_key))


def _compatibility_issues(
    identity: CompatibilityIdentity,
    supported: SupportedCompatibility,
    capabilities: tuple[CapabilityAnalysis, ...],
) -> tuple[ValidationIssue, ...]:
    output: list[ValidationIssue] = []
    for finding in analyze_compatibility(identity, supported):
        if finding.disposition is CompatibilityDisposition.COMPATIBLE:
            continue
        advisory = finding.disposition is CompatibilityDisposition.ADVISORY
        output.append(
            ValidationIssue(
                rule_id="compatibility.advisory" if advisory else "compatibility.unsupported",
                validator_rule_id=f"compatibility.identity.{finding.field}",
                phase=ValidationStage.COMPATIBILITY,
                severity=DiagnosticSeverity.SUGGESTION if advisory else DiagnosticSeverity.ERROR,
                blocking=not advisory,
                message=f"Compatibility identity {finding.field} is {finding.disposition.value}.",
                path=DiagnosticPath.json_pointer(f"/validator_stamp/{finding.field}"),
                notes=(f"supported={finding.supported}",),
                help="Use a validator and candidate whose explicit versions are supported.",
                documentation_reference="docs/configuration-validation.md",
            )
        )
    for analysis in capabilities:
        if analysis.compatible and analysis.disposition.value in {"satisfied", "fallback"}:
            continue
        output.append(
            ValidationIssue(
                rule_id=(
                    "compatibility.degraded"
                    if analysis.disposition.value in {"degraded", "degraded_fallback"}
                    else "compatibility.unsupported"
                ),
                validator_rule_id="compatibility.capability",
                phase=ValidationStage.COMPATIBILITY,
                severity=DiagnosticSeverity.ERROR if analysis.blocking else DiagnosticSeverity.WARNING,
                blocking=analysis.blocking,
                message=(f"Capability {analysis.need.name} is {analysis.disposition.value}."),
                path=DiagnosticPath.json_pointer(f"/capabilities/{analysis.need.name}"),
                notes=analysis.evidence,
                help="Provide an authorized compatible capability or a viable configured fallback.",
                documentation_reference="docs/configuration-validation.md",
            )
        )
    return tuple(sorted(output, key=ValidationIssue.sort_key))


def _preflight_issues(results: tuple[PreflightResult, ...]) -> tuple[ValidationIssue, ...]:
    output: list[ValidationIssue] = []
    for result in results:
        if result.status in {ProbeStatus.AVAILABLE, ProbeStatus.SKIPPED}:
            continue
        if result.failure_kind is ProbeFailureKind.TIMEOUT:
            rule_id = "preflight.timeout"
        elif result.blocking:
            rule_id = "preflight.dependency_unavailable"
        else:
            rule_id = "preflight.degraded"
        output.append(
            ValidationIssue(
                rule_id=rule_id,
                validator_rule_id="preflight.environment",
                phase=ValidationStage.PREFLIGHT,
                severity=DiagnosticSeverity.ERROR if result.blocking else DiagnosticSeverity.WARNING,
                blocking=result.blocking,
                message=f"Preflight {result.identifier}: {result.summary}",
                path=DiagnosticPath.json_pointer(f"/preflight/{result.identifier}"),
                notes=(f"status={result.status.value}", f"owner={result.owner}"),
                operational_effect=(
                    "Environmental readiness is blocked."
                    if result.blocking
                    else "The candidate remains valid but the dependency is degraded or optional."
                ),
                help="Inspect the explicitly configured dependency and retry preflight.",
                retryable=result.retryable,
                documentation_reference="docs/configuration-validation.md",
            )
        )
    return tuple(sorted(output, key=ValidationIssue.sort_key))


def evaluate_policy(
    stages: tuple[StageResult, ...],
    policy: ValidationPolicy,
) -> PolicyDecision:
    issues = tuple(issue for stage in stages for issue in stage.issues)
    valid, preflight_ready, acknowledgment = _policy_outcomes(stages, issues, policy)
    severity_counts, blocking_counts = _issue_counts(issues)
    return PolicyDecision(
        valid=valid,
        preflight_ready=preflight_ready,
        warning_acknowledgment_required=acknowledgment,
        acceptable_for_reload_decision=valid and preflight_ready and not acknowledgment,
        highest_severity=_highest_severity(issues),
        severity_counts=tuple(sorted(severity_counts.items())),
        blocking_counts=tuple(sorted(blocking_counts.items())),
        skipped_stages=_skipped_stage_summary(stages),
    )


def _policy_outcomes(
    stages: tuple[StageResult, ...],
    issues: tuple[ValidationIssue, ...],
    policy: ValidationPolicy,
) -> tuple[bool, bool, bool]:
    valid = not _has_blocking_issue(issues, _VALIDATION_PHASES)
    preflight = next(stage for stage in stages if stage.stage is ValidationStage.PREFLIGHT)
    preflight_ready = preflight.state is StageState.COMPLETED and not _has_blocking_issue(
        issues,
        frozenset({ValidationStage.PREFLIGHT}),
    )
    warnings = _has_warning(issues)
    if policy.warning_blocks and warnings:
        valid = False
    return valid, preflight_ready, warnings and policy.warning_acknowledgment_required


def _highest_severity(issues: tuple[ValidationIssue, ...]) -> DiagnosticSeverity | None:
    return max((issue.severity for issue in issues), key=_SEVERITY_ORDER.__getitem__, default=None)


def _skipped_stage_summary(stages: tuple[StageResult, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (stage.stage.value, stage.skipped_reason or "") for stage in stages if stage.state is StageState.SKIPPED
    )


def _has_blocking_issue(
    issues: tuple[ValidationIssue, ...],
    phases: frozenset[ValidationStage],
) -> bool:
    return any(issue.blocking and issue.phase in phases for issue in issues)


def _has_warning(issues: tuple[ValidationIssue, ...]) -> bool:
    return any(issue.severity is DiagnosticSeverity.WARNING for issue in issues)


def _issue_counts(issues: tuple[ValidationIssue, ...]) -> tuple[Counter[str], Counter[str]]:
    return (
        Counter(issue.severity.value for issue in issues),
        Counter("blocking" if issue.blocking else "nonblocking" for issue in issues),
    )


async def validate_compiled(
    compiled: CompiledConfiguration,
    *,
    context: ValidationContext | None = None,
) -> ValidationReport:
    selected = context or ValidationContext()
    deadline = time.monotonic() + VALIDATION_ENVELOPE_SECONDS
    started = selected.clock().astimezone(dt.UTC)
    _ensure_within_validation_envelope(deadline)
    candidate = CandidateIdentity.from_compiled(
        compiled,
        environment_inputs=selected.environment_inputs,
    )
    stages = await _validation_stages(compiled, selected, deadline=deadline)
    _ensure_within_validation_envelope(deadline)
    decision = evaluate_policy(stages, selected.policy)
    completed = selected.clock().astimezone(dt.UTC)
    stamp = _validator_stamp(
        compiled,
        selected,
        candidate=candidate,
        stages=stages,
        started=started,
        completed=completed,
    )
    report = ValidationReport(
        VALIDATION_REPORT_VERSION,
        candidate,
        stamp,
        stages,
        selected.policy,
        decision,
    )
    _ensure_within_validation_envelope(deadline)
    return report


def _ensure_within_validation_envelope(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise TimeoutError("validation exceeded its execution envelope")


async def _validation_stages(
    compiled: CompiledConfiguration,
    context: ValidationContext,
    *,
    deadline: float,
) -> tuple[StageResult, ...]:
    parse = StageResult(
        ValidationStage.PARSE,
        StageState.COMPLETED,
        _compiler_issues(compiled, ValidationStage.PARSE),
    )
    if not compiled.report.parse_valid:
        return _skipped_after(parse, "Parse validation failed.")
    schema = StageResult(
        ValidationStage.SCHEMA,
        StageState.COMPLETED,
        _compiler_issues(compiled, ValidationStage.SCHEMA),
    )
    if not compiled.report.schema_valid or compiled.value is None:
        return (parse, *_skipped_after(schema, "Schema validation failed."))
    return await _typed_stages(compiled, context, parse=parse, schema=schema, deadline=deadline)


async def _typed_stages(
    compiled: CompiledConfiguration,
    context: ValidationContext,
    *,
    parse: StageResult,
    schema: StageResult,
    deadline: float,
) -> tuple[StageResult, ...]:
    semantic = StageResult(
        ValidationStage.SEMANTIC,
        StageState.COMPLETED,
        validate_semantics(compiled),
    )
    identity = context.compatibility_identity or current_compatibility_identity(compiled.report.resolved_config_schema)
    supported = context.supported_compatibility or default_supported_compatibility()
    capabilities = analyze_capabilities(context.capability_views, context.capability_needs)
    compatibility = StageResult(
        ValidationStage.COMPATIBILITY,
        StageState.COMPLETED,
        _compatibility_issues(identity, supported, capabilities),
    )
    advisory_issues = evaluate_advisories(compiled)
    deprecation = StageResult(
        ValidationStage.DEPRECATION,
        StageState.COMPLETED,
        tuple(issue for issue in advisory_issues if issue.phase is ValidationStage.DEPRECATION),
    )
    advisory = StageResult(
        ValidationStage.ADVISORY,
        StageState.COMPLETED,
        tuple(issue for issue in advisory_issues if issue.phase is ValidationStage.ADVISORY),
    )
    preflight = await _preflight_stage(
        context,
        semantic=semantic,
        compatibility=compatibility,
        deadline=deadline,
    )
    return (parse, schema, semantic, compatibility, deprecation, advisory, preflight)


async def _preflight_stage(
    context: ValidationContext,
    *,
    semantic: StageResult,
    compatibility: StageResult,
    deadline: float,
) -> StageResult:
    if not context.preflight_enabled:
        return StageResult(
            ValidationStage.PREFLIGHT,
            StageState.SKIPPED,
            skipped_reason="Environmental preflight was not requested.",
        )
    if any(issue.blocking for issue in (*semantic.issues, *compatibility.issues)):
        return StageResult(
            ValidationStage.PREFLIGHT,
            StageState.SKIPPED,
            skipped_reason="Candidate semantic or compatibility validation failed.",
        )
    probe_results = await run_preflight(
        context.preflight_probes,
        executor=context.preflight_executor,
        deadline=deadline,
    )
    return StageResult(
        ValidationStage.PREFLIGHT,
        StageState.COMPLETED,
        _preflight_issues(probe_results),
        preflight_results=probe_results,
    )


def _validator_stamp(
    compiled: CompiledConfiguration,
    context: ValidationContext,
    *,
    candidate: CandidateIdentity,
    stages: tuple[StageResult, ...],
    started: dt.datetime,
    completed: dt.datetime,
) -> ValidatorStamp:
    rule_ids = expected_rule_identities(tuple((stage.stage, stage.state) for stage in stages))
    probe_ids = tuple(
        result.identifier
        for stage in stages
        if stage.stage is ValidationStage.PREFLIGHT
        for result in stage.preflight_results
    )
    payload_versions = tuple(sorted({policy.payload_schema_version for policy in JOB_TYPE_POLICIES.values()}))
    result_versions = tuple(sorted({policy.result_schema_version for policy in JOB_TYPE_POLICIES.values()}))
    return ValidatorStamp(
        stamp_version=VALIDATOR_STAMP_VERSION,
        software_version=__version__,
        build_identity=context.build_identity,
        validation_protocol_version=VALIDATION_PROTOCOL_VERSION,
        supported_config_schema=IntegerRange(
            min(SUPPORTED_CONFIG_SCHEMAS),
            max(SUPPORTED_CONFIG_SCHEMAS),
        ),
        selected_config_schema=compiled.report.resolved_config_schema,
        swwp_protocol_version=PROTOCOL_VERSION,
        job_payload_schema_versions=payload_versions,
        job_result_schema_versions=result_versions,
        diagnostic_schema_version=DIAGNOSTIC_SCHEMA_VERSION,
        diagnostic_catalog_version=DIAGNOSTIC_CATALOG_VERSION,
        capability_manifest_version=MANIFEST_SCHEMA_VERSION,
        candidate_sha256=candidate.sha256,
        candidate_identity_sha256=candidate.identity_sha256,
        active_configuration_generation=context.active_configuration_generation,
        started_at=started,
        completed_at=completed,
        rule_identities=rule_ids,
        probe_identities=probe_ids,
    )


def _skipped_after(
    completed: StageResult,
    reason: str,
) -> tuple[StageResult, ...]:
    prefix_index = STAGE_ORDER.index(completed.stage)
    prefix = (completed,)
    suffix = tuple(
        StageResult(stage, StageState.SKIPPED, skipped_reason=reason) for stage in STAGE_ORDER[prefix_index + 1 :]
    )
    return (*prefix, *suffix)


async def validate_path(
    path: str,
    *,
    context: ValidationContext | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[CompiledConfiguration, ValidationReport]:
    compiled = compile_path(path, environ=environ)
    return compiled, await validate_compiled(compiled, context=context)


def verify_report(
    report: ValidationReport,
    *,
    expected_candidate_sha256: str | None,
    expected_candidate_identity_sha256: str,
    expected_report_sha256: str,
    current_active_generation: int | None = None,
    require_fresh_generation: bool = False,
    supported: SupportedCompatibility | None = None,
) -> ReportVerification:
    return verify_report_mapping(
        report.to_dict(),
        expected_candidate_sha256=expected_candidate_sha256,
        expected_candidate_identity_sha256=expected_candidate_identity_sha256,
        expected_report_sha256=expected_report_sha256,
        current_active_generation=current_active_generation,
        require_fresh_generation=require_fresh_generation,
        supported=supported,
    )


def verify_report_mapping(
    payload: Mapping[str, object],
    *,
    expected_candidate_sha256: str | None,
    expected_candidate_identity_sha256: str,
    expected_report_sha256: str,
    current_active_generation: int | None = None,
    require_fresh_generation: bool = False,
    supported: SupportedCompatibility | None = None,
) -> ReportVerification:
    """Fail-closed admission boundary for an externally supplied JSON report."""

    from .report_verifier import verify_external_mapping

    raw_failures = verify_external_mapping(
        payload,
        expected_candidate_sha256=expected_candidate_sha256,
        expected_candidate_identity_sha256=expected_candidate_identity_sha256,
        expected_report_sha256=expected_report_sha256,
        current_active_generation=current_active_generation,
        require_fresh_generation=require_fresh_generation,
        supported=supported or default_supported_compatibility(),
        report_version=VALIDATION_REPORT_VERSION,
        stamp_version=VALIDATOR_STAMP_VERSION,
        validation_protocol_version=VALIDATION_PROTOCOL_VERSION,
    )
    ordered = tuple(VerificationFailure(item) for item in raw_failures)
    return ReportVerification(not ordered, ordered)


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
