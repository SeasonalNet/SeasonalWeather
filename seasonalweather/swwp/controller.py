"""Deterministic controller-side SWWP/1 session state machine."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any, Protocol

from ..capabilities.probes import CapabilityProbe as DomainCapabilityProbe
from ..capabilities.probes import ProbeMode, ProbeReason
from ..capabilities.registry import WorkerCapabilitySnapshot
from ..jobs.policies import JobType, QueueClass
from .adapter import DurableSwwpPort, remote_executors
from .auth import (
    AuthenticatedPrincipal,
    ControllerVersionSupport,
    RegistrationPolicy,
    negotiate_subprotocol,
    negotiate_versions,
)
from .capability_adapter import manifest_from_wire, record_from_wire, update_from_wire
from .constants import (
    DEFAULT_LIMITS,
    SUBPROTOCOL,
    ControllerState,
    ProtocolErrorCategory,
    ProtocolLimits,
    WorkerReadinessState,
)
from .messages import (
    Cancel,
    CancelAcknowledged,
    CapabilityProbe,
    CapabilityRecoveryAction,
    CapabilityReport,
    CapabilityUpdate,
    CapabilityUpdateAck,
    Drain,
    Drained,
    Envelope,
    Heartbeat,
    HeartbeatAck,
    JobAccepted,
    JobFailed,
    JobProgress,
    JobRejected,
    JobResult,
    LeaseRef,
    Payload,
    Reconcile,
    ReconcileResult,
    Register,
    Registered,
    RegistrationRejected,
    ResultCommitted,
    SelectedVersions,
    WorkerDiagnostic,
    WorkerDiagnosticAck,
    WorkerTelemetry,
    WorkerTelemetryAck,
)
from .session import SessionMachine

_TERMINAL = {ControllerState.CLOSED, ControllerState.REJECTED, ControllerState.FAILED}


class StaleSessionError(PermissionError):
    pass


class CapabilityControllerPort(Protocol):
    def register(self, **kwargs: Any) -> WorkerCapabilitySnapshot: ...

    def update(self, worker_id: str, **kwargs: Any): ...

    def heartbeat(self, worker_id: str, **kwargs: Any): ...

    def report(self, worker_id: str, **kwargs: Any): ...

    def request_probe(
        self,
        worker_id: str,
        *,
        mode: ProbeMode,
        reason: ProbeReason,
        names: tuple[str, ...] = (),
    ) -> DomainCapabilityProbe: ...

    def take_probes(self, worker_id: str) -> tuple[DomainCapabilityProbe, ...]: ...

    def reject_unacknowledged(
        self,
        lease: LeaseRef,
        *,
        category: str,
        capability_names: tuple[str, ...],
    ) -> object: ...

    def disconnect(self, worker_id: str, *, session_id: str) -> None: ...

    def drain(self, worker_id: str) -> None: ...

    def acquire(self, **kwargs: Any): ...

    def acknowledge(self, lease: LeaseRef) -> object: ...

    def renew(self, lease: LeaseRef) -> object: ...

    def progress(self, progress: JobProgress) -> None: ...

    def result(self, result: JobResult): ...

    def failure(self, failure: JobFailed): ...

    def request_cancellation(self, job_id: str) -> object: ...

    def reconcile(self, item: Any): ...

    def reconcile_repository(self) -> None: ...


class WorkerDiagnosticPort(Protocol):
    def handle(
        self,
        diagnostic: WorkerDiagnostic,
        *,
        worker_id: str,
        worker_instance_id: str,
        session_id: str,
        worker_epoch: int,
    ) -> WorkerDiagnosticAck: ...

    def release_session(
        self,
        *,
        worker_id: str,
        worker_instance_id: str,
        session_id: str,
        worker_epoch: int,
    ) -> None: ...


class WorkerTelemetryPort(Protocol):
    def handle(
        self,
        telemetry: WorkerTelemetry,
        *,
        worker_id: str,
        worker_instance_id: str,
        session_id: str,
        worker_epoch: int,
    ) -> tuple[int, int, str]: ...


class ControllerSession(SessionMachine):
    def __init__(
        self,
        *,
        controller_epoch: int,
        offered_subprotocols: tuple[str, ...],
        policy: RegistrationPolicy,
        durable: DurableSwwpPort,
        id_factory: Callable[[str], str],
        clock: Callable[[], dt.datetime],
        version_support: ControllerVersionSupport | None = None,
        limits: ProtocolLimits = DEFAULT_LIMITS,
        heartbeat_interval_seconds: int = 15,
        heartbeat_timeout_seconds: int = 45,
        lease_seconds: int = 60,
        assignment_ack_seconds: int = 10,
        capabilities: CapabilityControllerPort | None = None,
        diagnostics: WorkerDiagnosticPort | None = None,
        telemetry: WorkerTelemetryPort | None = None,
        traceparent: str | None = None,
    ) -> None:
        super().__init__(clock=clock, id_factory=id_factory, limits=limits, traceparent=traceparent)
        if controller_epoch < 1:
            raise ValueError("controller_epoch must be positive")
        self.controller_epoch = controller_epoch
        self.offered_subprotocols = offered_subprotocols
        self.policy = policy
        self.durable = durable
        self.version_support = version_support or ControllerVersionSupport()
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.lease_seconds = lease_seconds
        self.assignment_ack_seconds = assignment_ack_seconds
        self.capabilities = capabilities
        self.diagnostics = diagnostics
        self.telemetry = telemetry
        self.state = ControllerState.AWAITING_REGISTRATION
        self.session_id: str | None = None
        self.worker_id: str | None = None
        self.worker_instance_id: str | None = None
        self.worker_epoch: int | None = None
        self.accepted_queues: tuple[QueueClass, ...] = ()
        self.authorized_job_types: tuple[JobType, ...] = ()
        self.authorized_capabilities: tuple[str, ...] = ()
        self.selected_versions: SelectedVersions | None = None
        self.assignments: dict[tuple[str, str, str, int], str] = {}
        self.last_heartbeat_at: dt.datetime | None = None
        self.worker_lifecycle_state = WorkerReadinessState.STARTING
        self.worker_ready = False
        self.worker_accepting_new_jobs = False

    def _out(self, payload: Payload) -> Envelope:
        return self.envelope(
            payload,
            session_id=self.session_id,
            worker_id=self.worker_id,
            worker_instance_id=self.worker_instance_id,
            controller_epoch=self.controller_epoch,
            worker_epoch=self.worker_epoch,
        )

    def _work_port(self) -> Any:
        return self.capabilities or self.durable

    def receive(self, incoming: Envelope) -> tuple[Envelope, ...]:
        if self.state in _TERMINAL:
            return ()
        try:
            replay = self.replay(incoming)
            if replay is not None:
                return replay
            if self.state is ControllerState.AWAITING_REGISTRATION:
                responses = self._register(incoming)
            else:
                self._validate_session(incoming)
                responses = self._active(incoming)
        except StaleSessionError:
            responses = self._error_response(
                incoming,
                ProtocolErrorCategory.STALE_SESSION,
                "message belongs to a stale or mismatched session",
                fatal=True,
            )
        except PermissionError:
            responses = self._reject_or_error(
                incoming,
                ProtocolErrorCategory.UNAUTHORIZED,
                "registration or message identity is not authorized",
                fatal=True,
            )
        except KeyError:
            responses = self._error_response(
                incoming,
                ProtocolErrorCategory.STALE_LEASE,
                "message does not match a current durable lease",
                fatal=False,
            )
        except ValueError as exc:
            category = (
                ProtocolErrorCategory.RATE_SEQUENCE
                if "duplicate" in str(exc)
                else ProtocolErrorCategory.STATE_VIOLATION
            )
            responses = self._error_response(incoming, category, "message is invalid for session state", fatal=True)
        except Exception:
            responses = self._error_response(
                incoming,
                ProtocolErrorCategory.INTERNAL_REJECTION,
                "controller rejected the message safely",
                fatal=True,
            )
        self.remember(incoming, responses)
        return responses

    def _register(self, incoming: Envelope) -> tuple[Envelope, ...]:
        if not isinstance(incoming.payload, Register):
            return self._error_response(
                incoming,
                ProtocolErrorCategory.REGISTRATION_REQUIRED,
                "first application message must be register",
                fatal=True,
            )
        registration = incoming.payload
        if not self._registration_identity_matches(incoming, registration):
            return self._reject_registration(
                incoming,
                ProtocolErrorCategory.UNAUTHORIZED,
                "registration envelope identity does not match payload",
            )
        try:
            subprotocol = negotiate_subprotocol(self.offered_subprotocols)
            principal = self.policy.authorize(registration, self.clock())
            selected = negotiate_versions(registration, self.version_support)
        except PermissionError:
            return self._reject_registration(
                incoming,
                ProtocolErrorCategory.UNAUTHORIZED,
                "transport principal is not authorized",
            )
        except ValueError:
            return self._reject_registration(
                incoming,
                ProtocolErrorCategory.UNSUPPORTED_VERSION,
                "SWWP subprotocol or required schema versions are incompatible",
            )
        queues, authorized_jobs, capabilities = self._registration_grants(
            registration,
            principal,
            selected,
        )
        session_id = self.id_factory("session")
        try:
            capability_snapshot = self._register_capabilities(
                registration,
                session_id,
                capabilities,
                authorized_jobs,
                selected,
            )
        except (PermissionError, ValueError):
            return self._reject_registration(
                incoming,
                ProtocolErrorCategory.UNAUTHORIZED,
                "capability manifest is unauthorized or invalid",
            )
        self.session_id = session_id
        self.worker_id = registration.worker_id
        self.worker_instance_id = registration.worker_instance_id
        self.worker_epoch = registration.worker_epoch
        self.worker_lifecycle_state = registration.lifecycle_state
        self.worker_ready = registration.ready
        self.worker_accepting_new_jobs = registration.accepting_new_jobs
        self.accepted_queues = tuple(sorted(queues, key=lambda item: item.value))
        self.authorized_job_types = tuple(sorted(authorized_jobs, key=lambda item: item.value))
        self.authorized_capabilities = tuple(sorted(capabilities))
        self.selected_versions = selected
        self.state = ControllerState.ACTIVE
        response = Registered(
            session_id=self.session_id,
            controller_epoch=self.controller_epoch,
            selected_subprotocol=subprotocol,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
            lease_seconds=self.lease_seconds,
            assignment_ack_seconds=self.assignment_ack_seconds,
            accepted_queues=self.accepted_queues,
            authorized_job_types=self.authorized_job_types,
            authorized_capabilities=self.authorized_capabilities,
            selected_versions=selected,
            max_message_bytes=self.limits.max_message_bytes,
            max_active_assignments=registration.requested_slots,
            effective_capabilities=(
                capability_snapshot.schedulable_capabilities
                if capability_snapshot is not None
                else self.authorized_capabilities
            ),
            capability_epoch=registration.capability_manifest.epoch,
            capability_digest=registration.capability_manifest.digest,
            qualification_required=(capability_snapshot.probe_required if capability_snapshot is not None else False),
        )
        return (self._out(response), *self._capability_probe_frames())

    @staticmethod
    def _registration_grants(
        registration: Register,
        principal: AuthenticatedPrincipal,
        selected: SelectedVersions,
    ) -> tuple[set[QueueClass], set[JobType], set[str]]:
        queues = set(registration.requested_queues).intersection(principal.queues) - {QueueClass.CONTROL}
        advertised_jobs = set(selected.job_payloads).intersection(selected.job_results)
        authorized_jobs = {
            job_type
            for job_type in principal.job_types.intersection(advertised_jobs)
            if not job_type.value.startswith("control.")
        }
        capabilities = set(registration.capability_manifest.names).intersection(principal.capabilities)
        return queues, authorized_jobs, capabilities

    def _register_capabilities(
        self,
        registration: Register,
        session_id: str,
        capabilities: set[str],
        authorized_jobs: set[JobType],
        selected: SelectedVersions,
    ) -> WorkerCapabilitySnapshot | None:
        if self.capabilities is None:
            return None
        return self.capabilities.register(
            worker_id=registration.worker_id,
            worker_instance_id=registration.worker_instance_id,
            session_id=session_id,
            manifest=manifest_from_wire(registration.capability_manifest),
            authorized_capabilities=frozenset(capabilities),
            authorized_job_types=frozenset(authorized_jobs),
            payload_versions=selected.job_payloads,
            result_versions=selected.job_results,
        )

    @staticmethod
    def _registration_identity_matches(incoming: Envelope, registration: Register) -> bool:
        return all(
            (
                incoming.worker_id == registration.worker_id,
                incoming.worker_instance_id == registration.worker_instance_id,
                incoming.worker_epoch == registration.worker_epoch,
                incoming.session_id is None,
                incoming.controller_epoch is None,
            )
        )

    def _reject_registration(
        self,
        incoming: Envelope,
        category: ProtocolErrorCategory,
        summary: str,
    ) -> tuple[Envelope, ...]:
        self.state = ControllerState.REJECTED
        payload = RegistrationRejected(
            category=category,
            summary=summary,
            supported_subprotocols=(SUBPROTOCOL,),
            supported_swwp_versions=(1,),
        )
        return (
            self.envelope(
                payload,
                controller_epoch=self.controller_epoch,
                worker_id=incoming.worker_id,
                worker_instance_id=incoming.worker_instance_id,
                worker_epoch=incoming.worker_epoch,
            ),
        )

    def _reject_or_error(
        self,
        incoming: Envelope,
        category: ProtocolErrorCategory,
        summary: str,
        *,
        fatal: bool,
    ) -> tuple[Envelope, ...]:
        if self.state is ControllerState.AWAITING_REGISTRATION:
            return self._reject_registration(incoming, category, summary)
        return self._error_response(incoming, category, summary, fatal=fatal)

    def _error_response(
        self,
        incoming: Envelope,
        category: ProtocolErrorCategory,
        summary: str,
        *,
        fatal: bool,
    ) -> tuple[Envelope, ...]:
        if fatal:
            self.state = ControllerState.FAILED
        return (
            self.error(
                category,
                summary,
                correlated=incoming.message_id,
                fatal=fatal,
                session_id=self.session_id,
                worker_id=self.worker_id,
                worker_instance_id=self.worker_instance_id,
                controller_epoch=self.controller_epoch,
                worker_epoch=self.worker_epoch,
            ),
        )

    def _validate_session(self, incoming: Envelope) -> None:
        if (
            incoming.session_id != self.session_id
            or incoming.controller_epoch != self.controller_epoch
            or incoming.worker_id != self.worker_id
            or incoming.worker_instance_id != self.worker_instance_id
            or incoming.worker_epoch != self.worker_epoch
        ):
            raise StaleSessionError("stale or mismatched session identity")

    def _active(self, incoming: Envelope) -> tuple[Envelope, ...]:
        payload = incoming.payload
        handlers: dict[type[object], Callable[[Any], tuple[Envelope, ...]]] = {
            Heartbeat: self._heartbeat,
            JobAccepted: self._job_accepted,
            JobRejected: self._job_rejected,
            JobProgress: self._job_progress,
            JobResult: self._job_result,
            JobFailed: self._job_failed,
            CancelAcknowledged: self._no_response,
            Drained: self._drained,
            Reconcile: self._reconcile,
            CapabilityUpdate: self._capability_update,
            CapabilityReport: self._capability_report,
            WorkerDiagnostic: self._worker_diagnostic,
            WorkerTelemetry: self._worker_telemetry,
        }
        handler = handlers.get(type(payload))
        if handler is None:
            raise ValueError("message type is not accepted from worker")
        return handler(payload)

    def _heartbeat(self, payload: Heartbeat) -> tuple[Envelope, ...]:
        renewed = []
        reconcile = []
        for lease in payload.active_leases:
            if self.assignments.get(self._lease_key(lease)) != "accepted":
                reconcile.append(lease)
                continue
            try:
                self._work_port().renew(lease)
                renewed.append(lease)
            except (KeyError, ValueError, RuntimeError):
                reconcile.append(lease)
        self.last_heartbeat_at = self.clock()
        self.worker_lifecycle_state = payload.lifecycle_state
        self.worker_ready = payload.ready
        self.worker_accepting_new_jobs = payload.accepting_new_jobs
        if (
            self.capabilities is not None
            and self.worker_id is not None
            and self.session_id is not None
            and self.worker_instance_id is not None
            and payload.capability_epoch is not None
            and payload.capability_digest is not None
        ):
            self.capabilities.heartbeat(
                self.worker_id,
                session_id=self.session_id,
                worker_instance_id=self.worker_instance_id,
                epoch=payload.capability_epoch,
                digest=payload.capability_digest,
            )
        return (
            self._out(HeartbeatAck(renewed=tuple(renewed), reconcile=tuple(reconcile))),
            *self._capability_probe_frames(),
        )

    def readiness_snapshot(self) -> dict[str, bool | str]:
        """Return bounded worker readiness observed over SWWP."""
        return {
            "state": self.worker_lifecycle_state.value,
            "ready": self.worker_ready,
            "accepting_new_jobs": self.worker_accepting_new_jobs,
            "heartbeat_current": self.last_heartbeat_at is not None,
        }

    def _job_accepted(self, payload: JobAccepted) -> tuple[Envelope, ...]:
        key = self._lease_key(payload.lease)
        prior = self.assignments.get(key)
        if prior == "rejected":
            raise ValueError("conflicting assignment decision")
        if prior != "accepted":
            self._work_port().acknowledge(payload.lease)
            self.assignments[key] = "accepted"
        return ()

    def _worker_diagnostic(self, payload: WorkerDiagnostic) -> tuple[Envelope, ...]:
        if (
            self.diagnostics is None
            or self.worker_id is None
            or self.worker_instance_id is None
            or self.session_id is None
            or self.worker_epoch is None
        ):
            raise PermissionError("worker diagnostics are not authorized for this session")
        try:
            acknowledgment = self.diagnostics.handle(
                payload,
                worker_id=self.worker_id,
                worker_instance_id=self.worker_instance_id,
                session_id=self.session_id,
                worker_epoch=self.worker_epoch,
            )
        except Exception:
            acknowledgment = WorkerDiagnosticAck(
                diagnostic_id=payload.diagnostic_id,
                accepted=False,
                summary="controller rejected worker diagnostic safely",
            )
        return (self._out(acknowledgment),)

    def _worker_telemetry(self, payload: WorkerTelemetry) -> tuple[Envelope, ...]:
        if (
            self.telemetry is None
            or self.worker_id is None
            or self.worker_instance_id is None
            or self.session_id is None
            or self.worker_epoch is None
        ):
            accepted, rejected, summary = 0, len(payload.samples), "worker telemetry is unavailable"
        else:
            try:
                accepted, rejected, summary = self.telemetry.handle(
                    payload,
                    worker_id=self.worker_id,
                    worker_instance_id=self.worker_instance_id,
                    session_id=self.session_id,
                    worker_epoch=self.worker_epoch,
                )
            except Exception:
                accepted, rejected, summary = 0, len(payload.samples), "worker telemetry rejected safely"
        return (
            self._out(
                WorkerTelemetryAck(
                    schema_version=payload.schema_version,
                    accepted=accepted,
                    rejected=rejected,
                    summary=summary,
                )
            ),
        )

    def _job_rejected(self, payload: JobRejected) -> tuple[Envelope, ...]:
        key = self._lease_key(payload.lease)
        if self.assignments.get(key) == "accepted":
            raise ValueError("conflicting assignment decision")
        if self.assignments.get(key) != "delivered":
            raise ValueError("rejection requires a delivered assignment")
        if self.capabilities is not None:
            self.capabilities.reject_unacknowledged(
                payload.lease,
                category=payload.category.value,
                capability_names=payload.capabilities,
            )
        else:
            self.durable.reject_unacknowledged(payload.lease)
        self.assignments[key] = "rejected"
        return self._capability_probe_frames()

    def _job_progress(self, payload: JobProgress) -> tuple[Envelope, ...]:
        self._require_accepted(payload.lease)
        self._work_port().progress(payload)
        return ()

    def _job_result(self, payload: JobResult) -> tuple[Envelope, ...]:
        self._require_accepted(payload.lease)
        receipt = self._work_port().result(payload)
        return (
            self._out(
                ResultCommitted(
                    lease=payload.lease,
                    completion_id=payload.completion_id,
                    result_hash=receipt.result_hash,
                    committed_at=receipt.committed_at,
                )
            ),
        )

    def _job_failed(self, payload: JobFailed) -> tuple[Envelope, ...]:
        self._require_accepted(payload.lease)
        self._work_port().failure(payload)
        return ()

    @staticmethod
    def _no_response(_: object) -> tuple[Envelope, ...]:
        return ()

    def _drained(self, payload: Drained) -> tuple[Envelope, ...]:
        if self.state is not ControllerState.DRAINING:
            raise ValueError("drained without controller drain")
        if not payload.active and not payload.unacknowledged_completions:
            self.state = ControllerState.CLOSED
        return ()

    def _reconcile(self, payload: Reconcile) -> tuple[Envelope, ...]:
        decisions = tuple(self._work_port().reconcile(item) for item in payload.items)
        return (self._out(ReconcileResult(decisions=decisions)),)

    def _capability_update(self, payload: CapabilityUpdate) -> tuple[Envelope, ...]:
        recovery = CapabilityRecoveryAction.NONE
        if (
            self.capabilities is not None
            and self.worker_id is not None
            and self.session_id is not None
            and self.worker_instance_id is not None
        ):
            disposition = self.capabilities.update(
                self.worker_id,
                session_id=self.session_id,
                worker_instance_id=self.worker_instance_id,
                update=update_from_wire(payload),
            )
            if disposition.value in {"conflict", "gap"}:
                recovery = CapabilityRecoveryAction.FULL_REPORT_REQUIRED
            elif disposition.value == "stale":
                recovery = CapabilityRecoveryAction.STALE_IGNORED
        return (
            self._out(
                CapabilityUpdateAck(
                    epoch=payload.epoch,
                    digest=payload.full_digest,
                    recovery_action=recovery,
                )
            ),
            *self._capability_probe_frames(),
        )

    def _capability_report(self, payload: CapabilityReport) -> tuple[Envelope, ...]:
        if (
            self.capabilities is None
            or self.worker_id is None
            or self.session_id is None
            or self.worker_instance_id is None
        ):
            return ()
        self.capabilities.report(
            self.worker_id,
            session_id=self.session_id,
            worker_instance_id=self.worker_instance_id,
            probe_id=payload.probe_id,
            schema_version=payload.schema_version,
            epoch=payload.epoch,
            records=tuple(record_from_wire(item) for item in payload.records),
            full_digest=payload.full_digest,
            validity_seconds=payload.validity_seconds,
        )
        return ()

    def _capability_probe_frames(self) -> tuple[Envelope, ...]:
        if self.capabilities is None or self.worker_id is None:
            return ()
        return tuple(
            self._out(
                CapabilityProbe(
                    probe_id=probe.probe_id,
                    full=probe.mode is ProbeMode.FULL,
                    names=probe.target_names,
                    reason=probe.reason.value,
                    deadline_at=probe.deadline_at,
                )
            )
            for probe in self.capabilities.take_probes(self.worker_id)
        )

    @staticmethod
    def _lease_key(lease: LeaseRef) -> tuple[str, str, str, int]:
        return lease.job_id, lease.lease_id, lease.attempt_id, lease.attempt

    def _require_accepted(self, lease: LeaseRef) -> None:
        if self.assignments.get(self._lease_key(lease)) != "accepted":
            raise ValueError("job message requires an accepted assignment")

    def assign_next(self) -> Envelope | None:
        if self.state is not ControllerState.ACTIVE or self.worker_id is None:
            return None
        scheduler = self.capabilities or self.durable
        assignment = scheduler.acquire(
            owner=self.worker_id,
            queues=self.accepted_queues,
            executors=remote_executors(self.authorized_job_types),
            capabilities=self.authorized_capabilities,
        )
        if assignment is None or assignment.job_type not in self.authorized_job_types:
            return None
        key = self._lease_key(assignment.lease)
        if getattr(assignment, "traceparent", None) is None and self.traceparent is not None:
            assignment = assignment.model_copy(update={"traceparent": self.traceparent})
        self.assignments[key] = "delivered"
        return self._out(assignment)

    def request_drain(self, *, deadline_at: dt.datetime, reason: str) -> Envelope:
        if self.state is not ControllerState.ACTIVE:
            raise ValueError("only an active session may drain")
        self.state = ControllerState.DRAINING
        if self.capabilities is not None and self.worker_id is not None:
            self.capabilities.drain(self.worker_id)
        return self._out(Drain(deadline_at=deadline_at, reason=reason))

    def request_cancel(
        self,
        lease: LeaseRef,
        *,
        deadline_at: dt.datetime,
        reason: str,
    ) -> Envelope:
        if self.state not in {ControllerState.ACTIVE, ControllerState.DRAINING}:
            raise ValueError("only a live session may carry cancellation")
        if self.assignments.get(self._lease_key(lease)) not in {"delivered", "accepted"}:
            raise ValueError("cancellation requires a current session assignment")
        self._work_port().request_cancellation(lease.job_id)
        return self._out(Cancel(lease=lease, deadline_at=deadline_at, reason=reason))

    def reconcile_missed_acknowledgments(self) -> None:
        self._work_port().reconcile_repository()

    def timed_out(self) -> bool:
        if self.state not in {ControllerState.ACTIVE, ControllerState.DRAINING}:
            return False
        if self.last_heartbeat_at is None:
            return False
        if self.clock() - self.last_heartbeat_at <= dt.timedelta(seconds=self.heartbeat_timeout_seconds):
            return False
        self._release_diagnostic_session()
        self.state = ControllerState.CLOSED
        return True

    def transport_lost(self) -> None:
        if self.state not in _TERMINAL:
            if self.capabilities is not None and self.worker_id is not None and self.session_id is not None:
                self.capabilities.disconnect(self.worker_id, session_id=self.session_id)
            self._release_diagnostic_session()
            self.state = ControllerState.CLOSED

    def _release_diagnostic_session(self) -> None:
        if (
            self.diagnostics is None
            or self.worker_id is None
            or self.worker_instance_id is None
            or self.session_id is None
            or self.worker_epoch is None
        ):
            return
        self.diagnostics.release_session(
            worker_id=self.worker_id,
            worker_instance_id=self.worker_instance_id,
            session_id=self.session_id,
            worker_epoch=self.worker_epoch,
        )
