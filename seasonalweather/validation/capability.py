"""Read-only compatibility analysis over controller-qualified capability views."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from seasonalweather.capabilities.models import (
    CapabilityRecord,
    CompatibilityState,
    OperationalState,
    ParameterValue,
    capability_key,
)
from seasonalweather.capabilities.qualification import WorkerQualificationView


class CapabilityDisposition(StrEnum):
    SATISFIED = "satisfied"
    FALLBACK = "fallback"
    DEGRADED_FALLBACK = "degraded_fallback"
    NOT_IMPLEMENTED = "not_implemented"
    INCOMPATIBLE = "incompatible"
    STALE = "stale_or_unknown"
    DISABLED = "disabled"
    DRAINING = "draining"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    NO_CAPACITY = "no_capacity"
    PARAMETER_MISMATCH = "parameter_mismatch"


@dataclass(frozen=True)
class CapabilityNeed:
    name: str
    required: bool = True
    broadcast_critical: bool = False
    parameters: tuple[tuple[str, str | int | bool], ...] = ()
    fallback: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", tuple(tuple(item) for item in self.parameters))
        capability_key(self.name)
        if self.fallback == self.name:
            raise ValueError("capability fallback cannot refer to the primary capability")
        if self.fallback is not None:
            capability_key(self.fallback, "fallback capability name")
        keys = tuple(key for key, _ in self.parameters)
        if len(self.parameters) > 16 or keys != tuple(sorted(set(keys))):
            raise ValueError("capability parameters must be bounded and sorted")


@dataclass(frozen=True)
class CapabilityAnalysis:
    need: CapabilityNeed
    disposition: CapabilityDisposition
    blocking: bool
    matched_capability: str | None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def usable(self) -> bool:
        return self.disposition in {
            CapabilityDisposition.SATISFIED,
            CapabilityDisposition.FALLBACK,
            CapabilityDisposition.DEGRADED,
            CapabilityDisposition.DEGRADED_FALLBACK,
        }

    @property
    def compatible(self) -> bool:
        return self.usable


def _matches(reported: ParameterValue, required: str | int | bool) -> bool:
    return required in reported if isinstance(reported, tuple) else reported == required


def _record_health_disposition(record: CapabilityRecord) -> CapabilityDisposition | None:
    if record.compatibility is CompatibilityState.INCOMPATIBLE:
        return CapabilityDisposition.INCOMPATIBLE
    if record.compatibility is CompatibilityState.UNKNOWN:
        return CapabilityDisposition.STALE
    return {
        OperationalState.DISABLED: CapabilityDisposition.DISABLED,
        OperationalState.DRAINING: CapabilityDisposition.DRAINING,
        OperationalState.UNAVAILABLE: CapabilityDisposition.UNAVAILABLE,
        OperationalState.UNKNOWN: CapabilityDisposition.STALE,
    }.get(record.operational_state)


def _admission_disposition(
    view: WorkerQualificationView,
    record: CapabilityRecord,
    parameters: tuple[tuple[str, str | int | bool], ...],
) -> CapabilityDisposition:
    mismatch = any(
        (reported := record.parameters.get(key)) is None or not _matches(reported, required)
        for key, required in parameters
    )
    if mismatch:
        return CapabilityDisposition.PARAMETER_MISMATCH
    if not record.accepting_new_jobs or view.effective_capacity.get(record.name, 0) <= 0:
        return CapabilityDisposition.NO_CAPACITY
    return CapabilityDisposition.SATISFIED


def _view_disposition(
    view: WorkerQualificationView,
    record: CapabilityRecord,
    parameters: tuple[tuple[str, str | int | bool], ...],
) -> CapabilityDisposition:
    if not view.connected or not view.trusted or view.probe_required:
        return CapabilityDisposition.STALE
    if health := _record_health_disposition(record):
        return health
    admission = _admission_disposition(view, record, parameters)
    if admission is not CapabilityDisposition.SATISFIED:
        return admission
    return (
        CapabilityDisposition.DEGRADED
        if record.operational_state is OperationalState.DEGRADED
        else CapabilityDisposition.SATISFIED
    )


def _record_disposition(
    views: tuple[WorkerQualificationView, ...],
    *,
    name: str,
    parameters: tuple[tuple[str, str | int | bool], ...],
) -> tuple[CapabilityDisposition, tuple[str, ...]]:
    admitted = _admitted_records(views, name=name)
    if not admitted:
        return CapabilityDisposition.NOT_IMPLEMENTED, ()
    priority = {
        CapabilityDisposition.NOT_IMPLEMENTED: 0,
        CapabilityDisposition.INCOMPATIBLE: 1,
        CapabilityDisposition.STALE: 2,
        CapabilityDisposition.DISABLED: 3,
        CapabilityDisposition.DRAINING: 4,
        CapabilityDisposition.UNAVAILABLE: 5,
        CapabilityDisposition.PARAMETER_MISMATCH: 6,
        CapabilityDisposition.NO_CAPACITY: 7,
        CapabilityDisposition.DEGRADED: 8,
        CapabilityDisposition.SATISFIED: 9,
    }
    ranked: list[tuple[tuple[object, ...], CapabilityDisposition, WorkerQualificationView]] = []
    for view, record in admitted:
        disposition = _view_disposition(view, record, parameters)
        ranked.append(
            (
                (
                    -priority[disposition],
                    -view.epoch,
                    view.worker_id,
                    view.worker_instance_id,
                    view.session_id,
                    view.digest,
                    _record_stable_key(record),
                ),
                disposition,
                view,
            )
        )
    ranked.sort(key=lambda item: item[0])
    _, best, selected = ranked[0]
    evidence = (
        (f"epoch={selected.epoch}", f"digest={selected.digest}")
        if best in {CapabilityDisposition.SATISFIED, CapabilityDisposition.DEGRADED}
        else ()
    )
    return best, evidence


def _admitted_records(
    views: tuple[WorkerQualificationView, ...],
    *,
    name: str,
) -> tuple[tuple[WorkerQualificationView, CapabilityRecord], ...]:
    output: list[tuple[WorkerQualificationView, CapabilityRecord]] = []
    for view in views:
        record = next((item for item in view.records if item.name == name), None)
        if record is not None and record.implemented and name in view.authorized_capabilities:
            output.append((view, record))
    return tuple(
        sorted(
            output,
            key=lambda item: (
                item[0].worker_id,
                item[0].worker_instance_id,
                item[0].session_id,
                -item[0].epoch,
                item[0].digest,
                _record_stable_key(item[1]),
            ),
        )
    )


def _record_stable_key(record: CapabilityRecord) -> tuple[object, ...]:
    return (
        record.name,
        record.implemented,
        record.compatibility.value,
        record.operational_state.value,
        record.accepting_new_jobs,
        record.total_capacity,
        record.reported_available,
        record.job_restrictions,
        tuple((key, repr(value)) for key, value in sorted(record.parameters.items())),
        record.validity_seconds,
        record.observed_at.isoformat(),
        record.published_at.isoformat(),
        tuple((key, value.value) for key, value in sorted(record.dependency_health.items())),
    )


def analyze_capabilities(
    views: tuple[WorkerQualificationView, ...],
    needs: tuple[CapabilityNeed, ...],
) -> tuple[CapabilityAnalysis, ...]:
    """Analyze snapshots without reserving capacity, probing, or mutating state."""

    output: list[CapabilityAnalysis] = []
    for need in sorted(needs, key=lambda item: item.name):
        disposition, evidence = _record_disposition(views, name=need.name, parameters=need.parameters)
        matched = (
            need.name if disposition in {CapabilityDisposition.SATISFIED, CapabilityDisposition.DEGRADED} else None
        )
        if disposition not in {CapabilityDisposition.SATISFIED, CapabilityDisposition.DEGRADED} and need.fallback:
            fallback_disposition, fallback_evidence = _record_disposition(
                views,
                name=need.fallback,
                parameters=need.parameters,
            )
            if fallback_disposition in {
                CapabilityDisposition.SATISFIED,
                CapabilityDisposition.DEGRADED,
            }:
                disposition = (
                    CapabilityDisposition.DEGRADED_FALLBACK
                    if fallback_disposition is CapabilityDisposition.DEGRADED
                    else CapabilityDisposition.FALLBACK
                )
                evidence = fallback_evidence
                matched = need.fallback
        output.append(
            CapabilityAnalysis(
                need=need,
                disposition=disposition,
                blocking=(
                    disposition
                    not in {
                        CapabilityDisposition.SATISFIED,
                        CapabilityDisposition.FALLBACK,
                        CapabilityDisposition.DEGRADED,
                        CapabilityDisposition.DEGRADED_FALLBACK,
                    }
                    and (need.required or need.broadcast_critical)
                ),
                matched_capability=matched,
                evidence=evidence,
            )
        )
    return tuple(output)
