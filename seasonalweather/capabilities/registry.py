"""Lock-owned ephemeral controller registry and conservative capacity accounting."""

from __future__ import annotations

import datetime as dt
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from ..jobs.policies import JobType
from .manifest import (
    CapabilityManifest,
    CapabilityUpdate,
    EpochDisposition,
    apply_update,
    compare_epoch,
)
from .models import CapabilityRecord, OperationalState
from .probes import CapabilityProbe, ProbeMatch, ProbeTracker
from .qualification import WorkerQualificationView, compatible_records


class ReservationState(StrEnum):
    PENDING = "pending"
    BOUND = "bound"
    ACTIVE = "active"


@dataclass(frozen=True)
class CapacityReservation:
    reservation_id: str
    worker_id: str
    worker_instance_id: str
    job_id: str
    capability_names: tuple[str, ...]
    snapshot_token: str
    created_at: dt.datetime
    expires_at: dt.datetime
    state: ReservationState = ReservationState.PENDING
    lease_key: tuple[str, str, str, int] | None = None


@dataclass(frozen=True)
class WorkerCapabilitySnapshot:
    worker_id: str
    worker_instance_id: str
    session_id: str
    epoch: int
    digest: str
    records: tuple[CapabilityRecord, ...]
    authorized_capabilities: frozenset[str]
    authorized_job_types: frozenset[JobType]
    payload_versions: dict[JobType, int]
    result_versions: dict[JobType, int]
    effective_capacity: dict[str, int]
    trusted: bool
    connected: bool
    probe_required: bool
    expires_at: dt.datetime
    pending_reservations: int
    active_assignments: int
    outstanding_probes: int
    last_requalification_reason: str | None

    @property
    def schedulable_capabilities(self) -> tuple[str, ...]:
        """Return the current effective capability names safe for new work."""

        if not self.connected or not self.trusted or self.probe_required:
            return ()
        return tuple(sorted(name for name, available in self.effective_capacity.items() if available > 0))

    def qualification_view(self) -> WorkerQualificationView:
        return WorkerQualificationView(
            worker_id=self.worker_id,
            worker_instance_id=self.worker_instance_id,
            session_id=self.session_id,
            epoch=self.epoch,
            digest=self.digest,
            records=self.records,
            authorized_capabilities=self.authorized_capabilities,
            authorized_job_types=self.authorized_job_types,
            payload_versions=dict(self.payload_versions),
            result_versions=dict(self.result_versions),
            effective_capacity=dict(self.effective_capacity),
            trusted=self.trusted,
            connected=self.connected,
            probe_required=self.probe_required,
        )


@dataclass(frozen=True)
class RegistryHealth:
    connected_workers: int
    qualified_workers: int
    unknown_workers: int
    stale_capabilities: int
    expiring_capabilities: int
    outstanding_probes: int
    pending_reservations: int
    active_assignments: int
    total_capacity: int
    effective_capacity: int
    state_counts: dict[str, int]

    def details(self) -> dict[str, int | str]:
        details: dict[str, int | str] = {
            "active_assignments": self.active_assignments,
            "connected_workers": self.connected_workers,
            "effective_capacity": self.effective_capacity,
            "outstanding_probes": self.outstanding_probes,
            "qualified_workers": self.qualified_workers,
            "stale_capabilities": self.stale_capabilities,
            "total_capacity": self.total_capacity,
            "unknown_workers": self.unknown_workers,
        }
        if self.pending_reservations:
            details["pending_reservations"] = self.pending_reservations
        return details


@dataclass
class _WorkerState:
    worker_id: str
    worker_instance_id: str
    session_id: str
    manifest: CapabilityManifest
    effective_records: tuple[CapabilityRecord, ...]
    authorized_capabilities: frozenset[str]
    authorized_job_types: frozenset[JobType]
    payload_versions: dict[JobType, int]
    result_versions: dict[JobType, int]
    refresh_at: dict[str, dt.datetime]
    expiry_at: dict[str, dt.datetime]
    trusted: bool = True
    connected: bool = True
    probe_required: bool = False
    draining: bool = False
    reservations: dict[str, CapacityReservation] = field(default_factory=dict)
    active: dict[tuple[str, str, str, int], tuple[str, ...]] = field(default_factory=dict)
    probes: ProbeTracker = field(default_factory=ProbeTracker)
    last_requalification_reason: str | None = None


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("registry time must be timezone-aware")
    return value.astimezone(dt.UTC)


def _manifest_names_allowed(manifest: CapabilityManifest, allowed: frozenset[str]) -> bool:
    return all(record.name in allowed for record in manifest.records)


def _reservation_snapshot_current(
    state: _WorkerState | None,
    *,
    worker_instance_id: str,
    expected_epoch: int,
    expected_digest: str,
) -> bool:
    return bool(
        state is not None
        and state.worker_instance_id == worker_instance_id
        and state.connected
        and not state.draining
        and state.trusted
        and not state.probe_required
        and state.manifest.epoch == expected_epoch
        and state.manifest.digest == expected_digest
    )


def _expire_worker(state: _WorkerState, now: dt.datetime) -> bool:
    expired_names = {name for name, expires_at in state.expiry_at.items() if now >= expires_at}
    if expired_names:
        state.effective_records = tuple(
            record.unknown() if record.name in expired_names else record for record in state.effective_records
        )
        state.trusted = False
        state.probe_required = True
        state.last_requalification_reason = "capability_stale"
    expired_reservations = tuple(
        key
        for key, reservation in state.reservations.items()
        if reservation.state is not ReservationState.ACTIVE and now >= reservation.expires_at
    )
    for key in expired_reservations:
        state.reservations.pop(key, None)
    return bool(expired_names)


def _accepted_full_report(
    state: _WorkerState,
    manifest: CapabilityManifest,
    disposition: EpochDisposition,
) -> tuple[bool, bool, bool]:
    recovery_advance = state.probe_required and not state.trusted and manifest.epoch > state.manifest.epoch
    restoring_same = disposition is EpochDisposition.IDEMPOTENT and (state.probe_required or not state.trusted)
    accepted = disposition in {EpochDisposition.ACCEPTED, EpochDisposition.IDEMPOTENT} or recovery_advance
    return accepted, restoring_same, recovery_advance


def _count_snapshots(
    snapshots: list[WorkerCapabilitySnapshot],
    predicate: Callable[[WorkerCapabilitySnapshot], bool],
) -> int:
    return sum(map(predicate, snapshots))


def _effective_capacity(snapshot: WorkerCapabilitySnapshot) -> int:
    return sum(snapshot.effective_capacity.values())


class CapabilityRegistry:
    """One in-memory authority with atomic reads and writes under an RLock."""

    def __init__(
        self,
        *,
        allowed_capabilities: frozenset[str],
        maximum_validity_seconds: int = 300,
    ) -> None:
        if not 1 <= maximum_validity_seconds <= 900:
            raise ValueError("maximum capability validity is out of bounds")
        self._allowed = allowed_capabilities
        self._maximum_validity = maximum_validity_seconds
        self._workers: dict[str, _WorkerState] = {}
        self._lock = threading.RLock()

    def register(
        self,
        *,
        worker_id: str,
        worker_instance_id: str,
        session_id: str,
        manifest: CapabilityManifest,
        authorized_capabilities: frozenset[str],
        authorized_job_types: frozenset[JobType],
        payload_versions: dict[JobType, int],
        result_versions: dict[JobType, int],
        now: dt.datetime,
    ) -> WorkerCapabilitySnapshot:
        now = _utc(now)
        if any(record.compatibility.value != "unknown" for record in manifest.records):
            raise ValueError("worker cannot declare controller compatibility")
        permitted = authorized_capabilities.intersection(self._allowed)
        if not _manifest_names_allowed(manifest, self._allowed):
            raise PermissionError("manifest contains undeclared capability name")
        effective = compatible_records(manifest.records, allowed_names=permitted)
        with self._lock:
            prior = self._workers.get(worker_id)
            reconnect = prior is not None and prior.worker_instance_id == worker_instance_id
            if reconnect:
                prior_state = cast(_WorkerState, prior)
                if manifest.epoch < prior_state.manifest.epoch:
                    raise ValueError("reconnect manifest epoch is stale")
            state = self._new_worker_state(
                worker_id,
                worker_instance_id,
                session_id,
                manifest,
                effective,
                permitted,
                authorized_job_types,
                payload_versions,
                result_versions,
                now,
                reconnect,
            )
            if reconnect:
                state.active = dict(cast(_WorkerState, prior).active)
            self._workers[worker_id] = state
            return self._snapshot(state, now)

    def _new_worker_state(
        self,
        worker_id: str,
        worker_instance_id: str,
        session_id: str,
        manifest: CapabilityManifest,
        effective: tuple[CapabilityRecord, ...],
        permitted: frozenset[str],
        authorized_job_types: frozenset[JobType],
        payload_versions: dict[JobType, int],
        result_versions: dict[JobType, int],
        now: dt.datetime,
        reconnect: bool,
    ) -> _WorkerState:
        refresh, expiry = self._freshness(effective, now)
        return _WorkerState(
            worker_id=worker_id,
            worker_instance_id=worker_instance_id,
            session_id=session_id,
            manifest=manifest,
            effective_records=effective,
            authorized_capabilities=permitted,
            authorized_job_types=authorized_job_types,
            payload_versions=dict(payload_versions),
            result_versions=dict(result_versions),
            refresh_at=refresh,
            expiry_at=expiry,
            trusted=not reconnect,
            probe_required=reconnect,
            last_requalification_reason="reconnect" if reconnect else None,
        )

    def _freshness(
        self,
        records: tuple[CapabilityRecord, ...],
        now: dt.datetime,
    ) -> tuple[dict[str, dt.datetime], dict[str, dt.datetime]]:
        refresh = {record.name: now for record in records}
        expiry = {
            record.name: now + dt.timedelta(seconds=min(record.validity_seconds, self._maximum_validity))
            for record in records
        }
        return refresh, expiry

    def apply_update(
        self,
        worker_id: str,
        *,
        session_id: str,
        worker_instance_id: str,
        update: CapabilityUpdate,
        now: dt.datetime,
    ) -> EpochDisposition:
        now = _utc(now)
        with self._lock:
            state = self._current(worker_id, session_id, worker_instance_id)
            result = apply_update(state.manifest, update)
            if result.disposition is EpochDisposition.IDEMPOTENT:
                return result.disposition
            if result.manifest is None:
                if result.requires_full_report:
                    self._invalidate(state, "epoch_or_digest_mismatch")
                return result.disposition
            if not _manifest_names_allowed(result.manifest, self._allowed):
                self._invalidate(state, "unauthorized_capability_update")
                raise PermissionError("update contains undeclared capability name")
            state.manifest = result.manifest
            state.effective_records = compatible_records(
                result.manifest.records,
                allowed_names=state.authorized_capabilities,
            )
            state.refresh_at, state.expiry_at = self._freshness(state.effective_records, now)
            state.trusted = True
            state.probe_required = False
            state.last_requalification_reason = None
            return result.disposition

    def apply_full_report(
        self,
        worker_id: str,
        *,
        session_id: str,
        worker_instance_id: str,
        manifest: CapabilityManifest,
        now: dt.datetime,
    ) -> EpochDisposition:
        now = _utc(now)
        with self._lock:
            state = self._current(worker_id, session_id, worker_instance_id)
            disposition = compare_epoch(
                state.manifest,
                epoch=manifest.epoch,
                digest=manifest.digest,
            )
            accepted, restoring_same, recovery_advance = _accepted_full_report(
                state,
                manifest,
                disposition,
            )
            if not accepted:
                self._invalidate(state, "full_report_epoch_or_digest_mismatch")
                return disposition
            if disposition is EpochDisposition.IDEMPOTENT and not restoring_same:
                return disposition
            if not _manifest_names_allowed(manifest, self._allowed):
                self._invalidate(state, "unauthorized_full_report")
                raise PermissionError("full report contains undeclared capability name")
            self._replace_manifest(state, manifest, now)
            return EpochDisposition.ACCEPTED if restoring_same or recovery_advance else disposition

    def _replace_manifest(
        self,
        state: _WorkerState,
        manifest: CapabilityManifest,
        now: dt.datetime,
    ) -> None:
        state.manifest = manifest
        state.effective_records = compatible_records(
            manifest.records,
            allowed_names=state.authorized_capabilities,
        )
        state.refresh_at, state.expiry_at = self._freshness(state.effective_records, now)
        state.trusted = True
        state.probe_required = False
        state.last_requalification_reason = None

    def heartbeat(
        self,
        worker_id: str,
        *,
        session_id: str,
        worker_instance_id: str,
        epoch: int,
        digest: str,
        now: dt.datetime,
    ) -> EpochDisposition:
        """Matching heartbeat refreshes only an existing trusted full manifest."""

        now = _utc(now)
        with self._lock:
            state = self._current(worker_id, session_id, worker_instance_id)
            disposition = compare_epoch(state.manifest, epoch=epoch, digest=digest)
            if disposition is EpochDisposition.IDEMPOTENT:
                if state.trusted and not state.probe_required:
                    state.refresh_at, state.expiry_at = self._freshness(
                        state.effective_records,
                        now,
                    )
                return disposition
            if disposition in {
                EpochDisposition.ACCEPTED,
                EpochDisposition.CONFLICT,
                EpochDisposition.GAP,
            }:
                self._invalidate(state, "heartbeat_epoch_or_digest_mismatch")
                return EpochDisposition.GAP if disposition is EpochDisposition.ACCEPTED else disposition
            return disposition

    def tick(self, now: dt.datetime) -> tuple[str, ...]:
        now = _utc(now)
        changed: list[str] = []
        with self._lock:
            for worker_id, state in self._workers.items():
                if _expire_worker(state, now):
                    changed.append(worker_id)
                if state.probes.expire(now):
                    self._invalidate(state, "probe_timeout")
        return tuple(sorted(set(changed)))

    def snapshots(self, now: dt.datetime) -> tuple[WorkerCapabilitySnapshot, ...]:
        now = _utc(now)
        with self._lock:
            return tuple(
                self._snapshot(state, now) for state in sorted(self._workers.values(), key=lambda item: item.worker_id)
            )

    def snapshot(self, worker_id: str, now: dt.datetime) -> WorkerCapabilitySnapshot | None:
        now = _utc(now)
        with self._lock:
            state = self._workers.get(worker_id)
            return self._snapshot(state, now) if state is not None else None

    def _snapshot(self, state: _WorkerState, now: dt.datetime) -> WorkerCapabilitySnapshot:
        reserved = Counter[str]()
        for reservation in state.reservations.values():
            if reservation.state in {ReservationState.PENDING, ReservationState.BOUND}:
                reserved.update(reservation.capability_names)
        active = Counter[str]()
        for names in state.active.values():
            active.update(names)
        capacities: dict[str, int] = {}
        for record in state.effective_records:
            controller_available = max(
                0,
                record.total_capacity - active[record.name] - reserved[record.name],
            )
            capacities[record.name] = min(record.state_capacity, controller_available)
        expires_at = min(state.expiry_at.values(), default=now)
        return WorkerCapabilitySnapshot(
            worker_id=state.worker_id,
            worker_instance_id=state.worker_instance_id,
            session_id=state.session_id,
            epoch=state.manifest.epoch,
            digest=state.manifest.digest,
            records=state.effective_records,
            authorized_capabilities=state.authorized_capabilities,
            authorized_job_types=state.authorized_job_types,
            payload_versions=dict(state.payload_versions),
            result_versions=dict(state.result_versions),
            effective_capacity=capacities,
            trusted=state.trusted,
            connected=state.connected and not state.draining,
            probe_required=state.probe_required,
            expires_at=expires_at,
            pending_reservations=sum(item.state is not ReservationState.ACTIVE for item in state.reservations.values()),
            active_assignments=len(state.active),
            outstanding_probes=len(state.probes.snapshot()),
            last_requalification_reason=state.last_requalification_reason,
        )

    def reserve(
        self,
        *,
        worker_id: str,
        worker_instance_id: str,
        reservation_id: str,
        job_id: str,
        capability_names: tuple[str, ...],
        snapshot_token: str,
        expected_epoch: int,
        expected_digest: str,
        now: dt.datetime,
        expires_at: dt.datetime,
    ) -> CapacityReservation:
        now, expires_at = _utc(now), _utc(expires_at)
        if not now < expires_at <= now + dt.timedelta(minutes=5):
            raise ValueError("reservation expiry is invalid")
        names = tuple(sorted(set(capability_names)))
        with self._lock:
            state = self._workers.get(worker_id)
            if not _reservation_snapshot_current(
                state,
                worker_instance_id=worker_instance_id,
                expected_epoch=expected_epoch,
                expected_digest=expected_digest,
            ):
                raise RuntimeError("qualification snapshot is stale")
            state = cast(_WorkerState, state)
            existing = state.reservations.get(reservation_id)
            if existing is not None:
                if existing.snapshot_token != snapshot_token or existing.job_id != job_id:
                    raise ValueError("conflicting reservation identity")
                return existing
            snapshot = self._snapshot(state, now)
            if any(snapshot.effective_capacity.get(name, 0) <= 0 for name in names):
                raise RuntimeError("capability capacity is unavailable")
            reservation = CapacityReservation(
                reservation_id=reservation_id,
                worker_id=worker_id,
                worker_instance_id=worker_instance_id,
                job_id=job_id,
                capability_names=names,
                snapshot_token=snapshot_token,
                created_at=now,
                expires_at=expires_at,
            )
            state.reservations[reservation_id] = reservation
            return reservation

    def bind(
        self,
        worker_id: str,
        reservation_id: str,
        *,
        lease_key: tuple[str, str, str, int],
    ) -> CapacityReservation:
        with self._lock:
            state = self._workers[worker_id]
            reservation = state.reservations[reservation_id]
            if reservation.state is ReservationState.ACTIVE:
                raise ValueError("active reservation is already consumed")
            bound = CapacityReservation(
                **{
                    **reservation.__dict__,
                    "state": ReservationState.BOUND,
                    "lease_key": lease_key,
                }
            )
            state.reservations[reservation_id] = bound
            return bound

    def activate(
        self,
        worker_id: str,
        reservation_id: str,
        *,
        lease_key: tuple[str, str, str, int],
    ) -> None:
        with self._lock:
            state = self._workers[worker_id]
            reservation = state.reservations[reservation_id]
            if reservation.lease_key != lease_key:
                raise ValueError("reservation lease identity does not match")
            state.active.setdefault(lease_key, reservation.capability_names)
            state.reservations.pop(reservation_id, None)

    def release_reservation(self, worker_id: str, reservation_id: str) -> None:
        with self._lock:
            state = self._workers.get(worker_id)
            if state is not None:
                state.reservations.pop(reservation_id, None)

    def release_active(
        self,
        worker_id: str,
        lease_key: tuple[str, str, str, int],
    ) -> None:
        with self._lock:
            state = self._workers.get(worker_id)
            if state is not None:
                state.active.pop(lease_key, None)

    def reconcile_active(
        self,
        worker_id: str,
        *,
        worker_instance_id: str,
        assignments: dict[tuple[str, str, str, int], tuple[str, ...]],
    ) -> None:
        """Replace observed use from matching P1-07/P1-08 reconciliation evidence."""

        with self._lock:
            state = self._workers[worker_id]
            if state.worker_instance_id != worker_instance_id:
                raise PermissionError("active assignment evidence belongs to a stale instance")
            state.active = {key: tuple(sorted(set(names))) for key, names in assignments.items()}
            state.reservations.clear()

    def reject_assignment(
        self,
        worker_id: str,
        *,
        lease_key: tuple[str, str, str, int],
        capability_names: tuple[str, ...],
        reason: str,
    ) -> None:
        with self._lock:
            state = self._workers[worker_id]
            state.active.pop(lease_key, None)
            for key, reservation in tuple(state.reservations.items()):
                if reservation.lease_key == lease_key:
                    state.reservations.pop(key, None)
            affected = set(capability_names)
            state.effective_records = tuple(
                record.unknown() if record.name in affected else record for record in state.effective_records
            )
            state.probe_required = True
            state.last_requalification_reason = reason[:64]
            state.reservations.clear()

    def add_probe(self, worker_id: str, probe: CapabilityProbe) -> None:
        with self._lock:
            self._workers[worker_id].probes.add(probe)

    def match_probe(
        self,
        worker_id: str,
        probe_id: str,
        *,
        session_id: str,
        worker_instance_id: str,
        now: dt.datetime,
    ) -> ProbeMatch:
        with self._lock:
            state = self._workers.get(worker_id)
            if state is None:
                raise KeyError("unknown worker")
            return state.probes.match(
                probe_id,
                session_id=session_id,
                worker_instance_id=worker_instance_id,
                now=_utc(now),
            )

    def disconnect(self, worker_id: str, *, session_id: str) -> None:
        with self._lock:
            state = self._workers.get(worker_id)
            if state is None or state.session_id != session_id:
                return
            state.connected = False
            state.probe_required = True
            state.trusted = False
            state.last_requalification_reason = "disconnect"
            state.reservations.clear()
            state.probes.close()

    def drain(self, worker_id: str) -> None:
        with self._lock:
            state = self._workers.get(worker_id)
            if state is None:
                return
            state.draining = True
            state.reservations.clear()
            state.effective_records = tuple(
                record.model_copy(
                    update={
                        "operational_state": OperationalState.DRAINING,
                        "accepting_new_jobs": False,
                    }
                )
                for record in state.effective_records
            )

    def close(self) -> None:
        with self._lock:
            for state in self._workers.values():
                state.connected = False
                state.draining = True
                state.reservations.clear()
                state.probes.close()

    def health(self, now: dt.datetime, *, expiring_within_seconds: int = 30) -> RegistryHealth:
        now = _utc(now)
        with self._lock:
            snapshots = [self._snapshot(state, now) for state in self._workers.values()]
            records = [record for snapshot in snapshots for record in snapshot.records]
            counts = Counter(record.operational_state.value for record in records)
            return RegistryHealth(
                connected_workers=_count_snapshots(snapshots, lambda item: item.connected),
                qualified_workers=_count_snapshots(
                    snapshots,
                    lambda item: item.connected and item.trusted and not item.probe_required,
                ),
                unknown_workers=_count_snapshots(
                    snapshots,
                    lambda item: not item.trusted or item.probe_required,
                ),
                stale_capabilities=counts[OperationalState.UNKNOWN.value],
                expiring_capabilities=self._expiring_count(now, expiring_within_seconds),
                outstanding_probes=sum(map(lambda item: item.outstanding_probes, snapshots)),
                pending_reservations=sum(map(lambda item: item.pending_reservations, snapshots)),
                active_assignments=sum(map(lambda item: item.active_assignments, snapshots)),
                total_capacity=sum(map(lambda record: record.total_capacity, records)),
                effective_capacity=sum(map(_effective_capacity, snapshots)),
                state_counts=dict(sorted(counts.items())),
            )

    def _expiring_count(self, now: dt.datetime, within_seconds: int) -> int:
        deadline = now + dt.timedelta(seconds=within_seconds)
        return sum(
            now < expires <= deadline for state in self._workers.values() for expires in state.expiry_at.values()
        )

    @staticmethod
    def _invalidate(state: _WorkerState, reason: str) -> None:
        state.trusted = False
        state.probe_required = True
        state.last_requalification_reason = reason[:64]
        state.reservations.clear()
        state.effective_records = tuple(record.unknown() for record in state.effective_records)

    def _current(
        self,
        worker_id: str,
        session_id: str,
        worker_instance_id: str,
    ) -> _WorkerState:
        state = self._workers.get(worker_id)
        if state is None or state.session_id != session_id or state.worker_instance_id != worker_instance_id:
            raise PermissionError("capability update belongs to a stale session")
        return state
