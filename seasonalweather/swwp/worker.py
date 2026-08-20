"""Deterministic worker-side SWWP/1 session state machine without execution."""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from ..capabilities.manifest import manifest_digest
from ..jobs.contracts import AttemptOutcome
from ..jobs.policies import FailureCategory
from .capability_adapter import record_from_wire
from .constants import ProtocolErrorCategory, WorkerState
from .messages import (
    Cancel,
    CancelAcknowledged,
    CapabilityProbe,
    CapabilityRecordPayload,
    CapabilityRejectionCategory,
    CapabilityReport,
    CapabilityUpdate,
    DiagnosticTransition,
    Drain,
    Drained,
    Envelope,
    Heartbeat,
    JobAccepted,
    JobAssignmentPayload,
    JobFailed,
    JobProgress,
    JobRejected,
    JobResult,
    LeaseRef,
    Payload,
    ProtocolErrorPayload,
    Reconcile,
    ReconcileItem,
    ReconcileResult,
    Register,
    Registered,
    RegistrationRejected,
    ResultCommitted,
    WorkerDiagnostic,
    WorkerDiagnosticAck,
)
from .session import SessionMachine

_TERMINAL = {WorkerState.CLOSED, WorkerState.FAILED}


class WorkerSession(SessionMachine):
    def __init__(
        self,
        *,
        registration: Register,
        id_factory: Callable[[str], str],
        clock: Callable[[], dt.datetime],
        accept_assignments: bool = True,
        assignment_acceptor: Callable[[JobAssignmentPayload], bool] | None = None,
    ) -> None:
        super().__init__(clock=clock, id_factory=id_factory)
        self.registration = registration
        self.accept_assignments = accept_assignments
        self.assignment_acceptor = assignment_acceptor
        self.state = WorkerState.DISCONNECTED
        self.session_id: str | None = None
        self.controller_epoch: int | None = None
        self.assignments: dict[tuple[str, str, str, int], JobAssignmentPayload] = {}
        self.completions: dict[tuple[str, str, str, int], JobResult] = {}
        self.cancelled: set[tuple[str, str, str, int]] = set()
        self.capability_manifest = registration.capability_manifest
        self.diagnostic_requests: OrderedDict[str, WorkerDiagnostic] = OrderedDict()
        self.diagnostic_acknowledgments: OrderedDict[str, WorkerDiagnosticAck] = OrderedDict()
        self.diagnostic_occurrences: dict[str, str] = {}

    def _out(self, payload: Payload) -> Envelope:
        return self.envelope(
            payload,
            session_id=self.session_id,
            worker_id=self.registration.worker_id,
            worker_instance_id=self.registration.worker_instance_id,
            controller_epoch=self.controller_epoch,
            worker_epoch=self.registration.worker_epoch,
        )

    def connect(self) -> Envelope:
        if self.state is not WorkerState.DISCONNECTED:
            raise ValueError("worker can connect only while disconnected")
        self.state = WorkerState.REGISTERING
        return self._out(self.registration)

    def receive(self, incoming: Envelope) -> tuple[Envelope, ...]:
        if self.state in _TERMINAL:
            return ()
        try:
            replay = self.replay(incoming)
            if replay is not None:
                return replay
            responses = self._receive(incoming)
        except ValueError:
            self.state = WorkerState.FAILED
            responses = (
                self._out(
                    ProtocolErrorPayload(
                        category=ProtocolErrorCategory.STATE_VIOLATION,
                        summary="controller message is invalid for worker session state",
                        correlated_message_id=incoming.message_id,
                        fatal=True,
                    )
                ),
            )
        self.remember(incoming, responses)
        return responses

    def _receive(self, incoming: Envelope) -> tuple[Envelope, ...]:
        payload = incoming.payload
        if self.state is WorkerState.REGISTERING:
            return self._registration_response(payload)
        self._validate_session(incoming)
        handlers: dict[type[object], Callable[[Any], tuple[Envelope, ...]]] = {
            JobAssignmentPayload: self._assignment,
            Cancel: self._cancel,
            Drain: self._drain,
            ResultCommitted: self._result_committed,
            ReconcileResult: self._reconciled,
            ProtocolErrorPayload: self._protocol_error,
            CapabilityProbe: self._capability_probe,
            WorkerDiagnosticAck: self._diagnostic_ack,
        }
        handler = handlers.get(type(payload))
        return handler(payload) if handler is not None else ()

    @staticmethod
    def _no_response(_: object) -> tuple[Envelope, ...]:
        return ()

    def diagnostic(self, payload: WorkerDiagnostic) -> Envelope:
        if self.state not in {WorkerState.ACTIVE, WorkerState.DRAINING}:
            raise ValueError("worker is not active")
        self.diagnostic_requests[payload.diagnostic_id] = payload
        self.diagnostic_requests.move_to_end(payload.diagnostic_id)
        self._trim_diagnostic_state()
        return self._out(payload)

    def _diagnostic_ack(self, payload: WorkerDiagnosticAck) -> tuple[Envelope, ...]:
        self.diagnostic_acknowledgments[payload.diagnostic_id] = payload
        self.diagnostic_acknowledgments.move_to_end(payload.diagnostic_id)
        request = self.diagnostic_requests.get(payload.diagnostic_id)
        if payload.accepted and payload.controller_occurrence_id is not None:
            if request is not None and request.transition is DiagnosticTransition.RESOLVED:
                self.diagnostic_occurrences.pop(payload.diagnostic_id, None)
            else:
                self.diagnostic_occurrences[payload.diagnostic_id] = payload.controller_occurrence_id
        self._trim_diagnostic_state()
        return ()

    def _trim_diagnostic_state(self) -> None:
        maximum = self.limits.max_retained_errors * 8
        while len(self.diagnostic_acknowledgments) > maximum:
            diagnostic_id, _ = self.diagnostic_acknowledgments.popitem(last=False)
            if diagnostic_id not in self.diagnostic_occurrences:
                self.diagnostic_requests.pop(diagnostic_id, None)
        while len(self.diagnostic_requests) > maximum:
            removable = next(
                (
                    diagnostic_id
                    for diagnostic_id in self.diagnostic_requests
                    if diagnostic_id not in self.diagnostic_occurrences
                ),
                None,
            )
            if removable is None:
                raise ValueError("active diagnostic relationship retention is full")
            self.diagnostic_requests.pop(removable, None)
            self.diagnostic_acknowledgments.pop(removable, None)

    def _registration_response(self, payload: object) -> tuple[Envelope, ...]:
        if isinstance(payload, RegistrationRejected):
            self.state = WorkerState.CLOSED
            return ()
        if not isinstance(payload, Registered):
            raise ValueError("registered must be first controller message")
        if payload.selected_subprotocol != "seasonalweather.worker.v1":
            raise ValueError("controller selected unexpected subprotocol")
        self.session_id = payload.session_id
        self.controller_epoch = payload.controller_epoch
        self.state = WorkerState.ACTIVE
        if (
            payload.capability_epoch != self.capability_manifest.epoch
            or payload.capability_digest != self.capability_manifest.digest
        ):
            raise ValueError("registered capability baseline does not match")
        return ()

    def _assignment(self, payload: JobAssignmentPayload) -> tuple[Envelope, ...]:
        if self.state is not WorkerState.ACTIVE:
            raise ValueError("assignment received while not active")
        key = self._key(payload.lease)
        prior = self.assignments.get(key)
        if prior is not None and prior != payload:
            raise ValueError("conflicting duplicate assignment")
        if not self.accept_assignments or (
            self.assignment_acceptor is not None and not self.assignment_acceptor(payload)
        ):
            return (
                self._out(
                    JobRejected(
                        lease=payload.lease,
                        category=(
                            CapabilityRejectionCategory.CAPACITY_UNAVAILABLE
                            if not self.accept_assignments
                            else CapabilityRejectionCategory.CAPABILITY_UNAVAILABLE
                        ),
                        summary=(
                            "simulated worker rejected assignment"
                            if not self.accept_assignments
                            else "worker profile cannot execute this assignment"
                        ),
                        capabilities=payload.capability_requirements,
                    )
                ),
            )
        self.assignments[key] = payload
        return (self._out(JobAccepted(lease=payload.lease)),)

    def _cancel(self, payload: Cancel) -> tuple[Envelope, ...]:
        self.cancelled.add(self._key(payload.lease))
        return (self._out(CancelAcknowledged(lease=payload.lease, observed_at=self.clock())),)

    def _drain(self, _: Drain) -> tuple[Envelope, ...]:
        self.state = WorkerState.DRAINING
        return (
            self._out(
                Drained(
                    active=tuple(item.lease for item in self.assignments.values()),
                    unacknowledged_completions=tuple(result.completion_id for result in self.completions.values()),
                )
            ),
        )

    def _result_committed(self, payload: ResultCommitted) -> tuple[Envelope, ...]:
        key = self._key(payload.lease)
        retained = self.completions.get(key)
        if retained is None or retained.completion_id != payload.completion_id:
            raise ValueError("result commitment does not match retained completion")
        del self.completions[key]
        self.assignments.pop(key, None)
        return ()

    def _reconciled(self, _: ReconcileResult) -> tuple[Envelope, ...]:
        self.state = WorkerState.ACTIVE
        return ()

    def _protocol_error(self, payload: ProtocolErrorPayload) -> tuple[Envelope, ...]:
        if payload.fatal:
            self.state = WorkerState.FAILED
        return ()

    def _capability_probe(self, payload: CapabilityProbe) -> tuple[Envelope, ...]:
        if self.state not in {WorkerState.ACTIVE, WorkerState.DRAINING}:
            raise ValueError("capability probe requires a live worker")
        records = self.capability_manifest.records
        if not payload.full:
            targets = set(payload.names)
            records = tuple(record for record in records if record.name in targets)
            if tuple(record.name for record in records) != payload.names:
                raise ValueError("targeted capability is not implemented")
        next_epoch = self.capability_manifest.epoch + 1
        self.capability_manifest = self.capability_manifest.model_copy(update={"epoch": next_epoch})
        return (
            self._out(
                CapabilityReport(
                    probe_id=payload.probe_id,
                    schema_version=self.capability_manifest.schema_version,
                    epoch=next_epoch,
                    records=records,
                    full_digest=self.capability_manifest.digest,
                    validity_seconds=min(
                        (record.validity_seconds for record in records),
                        default=1,
                    ),
                )
            ),
        )

    def _validate_session(self, incoming: Envelope) -> None:
        if (
            incoming.session_id != self.session_id
            or incoming.controller_epoch != self.controller_epoch
            or incoming.worker_id != self.registration.worker_id
            or incoming.worker_instance_id != self.registration.worker_instance_id
            or incoming.worker_epoch != self.registration.worker_epoch
        ):
            raise ValueError("stale session identity")

    @staticmethod
    def _key(lease: LeaseRef) -> tuple[str, str, str, int]:
        return (lease.job_id, lease.lease_id, lease.attempt_id, lease.attempt)

    def heartbeat(self) -> Envelope:
        if self.state not in {WorkerState.ACTIVE, WorkerState.DRAINING}:
            raise ValueError("worker is not active")
        return self._out(
            Heartbeat(
                active_leases=tuple(item.lease for item in self.assignments.values()),
                capability_epoch=self.capability_manifest.epoch,
                capability_digest=self.capability_manifest.digest,
            )
        )

    def capability_update(
        self,
        *,
        changed: tuple[CapabilityRecordPayload, ...] = (),
        removed: tuple[str, ...] = (),
        validity_seconds: int,
    ) -> Envelope:
        if self.state not in {WorkerState.ACTIVE, WorkerState.DRAINING}:
            raise ValueError("worker is not active")
        records = {record.name: record for record in self.capability_manifest.records}
        for name in removed:
            records.pop(name, None)
        for record in changed:
            records[record.name] = record
        normalized = tuple(sorted(records.values(), key=lambda item: item.name))
        digest = manifest_digest(
            schema_version=self.capability_manifest.schema_version,
            records=tuple(record_from_wire(item) for item in normalized),
        )
        epoch = self.capability_manifest.epoch + 1
        self.capability_manifest = self.capability_manifest.model_copy(
            update={
                "epoch": epoch,
                "digest": digest,
                "records": normalized,
            }
        )
        return self._out(
            CapabilityUpdate(
                epoch=epoch,
                changed=changed,
                removed=tuple(sorted(set(removed))),
                full_digest=digest,
                validity_seconds=validity_seconds,
            )
        )

    def progress(
        self,
        lease: LeaseRef,
        *,
        stage: str,
        reason: str | None = None,
        numeric: dict[str, int | float] | None = None,
    ) -> Envelope:
        if self._key(lease) not in self.assignments:
            raise KeyError("unknown assignment")
        return self._out(JobProgress(lease=lease, stage=stage, reason=reason, numeric=numeric or {}))

    def result(
        self,
        lease: LeaseRef,
        *,
        result_schema_version: int,
        result: dict[str, object],
        completion_id: str,
        artifact_refs: tuple[str, ...] = (),
    ) -> Envelope:
        if self._key(lease) not in self.assignments:
            raise KeyError("unknown assignment")
        payload = JobResult(
            lease=lease,
            result_schema_version=result_schema_version,
            result=result,
            completion_id=completion_id,
            artifact_refs=artifact_refs,
        )
        self.completions[self._key(lease)] = payload
        return self._out(payload)

    def failure(
        self,
        lease: LeaseRef,
        *,
        outcome: AttemptOutcome,
        category: FailureCategory,
        error_code: str,
        summary: str,
    ) -> Envelope:
        if self._key(lease) not in self.assignments:
            raise KeyError("unknown assignment")
        return self._out(
            JobFailed(
                lease=lease,
                outcome=outcome,
                category=category,
                error_code=error_code,
                summary=summary,
            )
        )

    def reconnect_report(self, *, prior_session_id: str | None, prior_controller_epoch: int | None) -> Envelope:
        if self.state is not WorkerState.ACTIVE:
            raise ValueError("worker must register new session before reconciliation")
        self.state = WorkerState.RECONCILING
        items = tuple(
            ReconcileItem(
                lease=assignment.lease,
                prior_session_id=prior_session_id,
                accepted=True,
                cancellation_observed=key in self.cancelled,
                completion_id=self.completions[key].completion_id if key in self.completions else None,
                result_schema_version=(
                    self.completions[key].result_schema_version if key in self.completions else None
                ),
                result=self.completions[key].result if key in self.completions else None,
            )
            for key, assignment in self.assignments.items()
        )
        return self._out(
            Reconcile(
                prior_session_id=prior_session_id,
                prior_controller_epoch=prior_controller_epoch,
                items=items,
            )
        )

    def transport_lost(self) -> None:
        if self.state not in _TERMINAL:
            self.state = WorkerState.DISCONNECTED
            self.session_id = None
            self.controller_epoch = None
            self.registration = self.registration.model_copy(update={"capability_manifest": self.capability_manifest})
            self.reset_message_sequence()
