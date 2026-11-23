from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pytest

from seasonalweather.job_store import (
    DurableJobService,
    JobDatabase,
    JobRepository,
    JobScheduler,
)
from seasonalweather.job_store.models import ResultCommitReceipt
from seasonalweather.jobs.contracts import AttemptOutcome, JobStatus
from seasonalweather.jobs.policies import ExecutorClass, FailureCategory, JobType, QueueClass
from seasonalweather.lifecycle import Lifecycle
from seasonalweather.swwp.adapter import JobStoreSwwpAdapter
from seasonalweather.swwp.auth import AuthenticatedPrincipal, StaticRegistrationPolicy
from seasonalweather.swwp.constants import ControllerState, ProtocolErrorCategory, WorkerState
from seasonalweather.swwp.controller import ControllerSession
from seasonalweather.swwp.messages import (
    JobAssignmentPayload,
    JobProgress,
    JobResult,
    LeaseRef,
    ProtocolErrorPayload,
    ReconcileDecision,
    ReconcileItem,
    Register,
    RegistrationRejected,
    ResultCommitted,
    VersionSupport,
)
from seasonalweather.swwp.worker import WorkerSession
from tests.support.capabilities import wire_manifest, wire_record
from tests.support.swwp_simulation import DeterministicIds, SimulatedClock, SimulatedPeers

NOW = dt.datetime(2026, 7, 24, 12, tzinfo=dt.UTC)
LEASE = LeaseRef(
    job_id="job_00000001",
    lease_id="lease_00000001",
    attempt_id="attempt_00000001",
    attempt=1,
)


def registration(
    *,
    worker_id: str = "worker_00000001",
    versions: VersionSupport | None = None,
) -> Register:
    return Register(
        worker_id=worker_id,
        worker_instance_id="instance_00000001",
        worker_epoch=1,
        software_version="0.17.0",
        build_identity="build_eaaf24b",
        requested_queues=(QueueClass.ROUTINE, QueueClass.CONTROL),
        requested_slots=2,
        capability_manifest=wire_manifest(
            (
                wire_record(
                    "tts.synthesis.v1",
                    now=NOW,
                    parameters={"format": "wav"},
                ),
                wire_record("untrusted.capability", now=NOW),
            )
        ),
        supported_versions=versions
        or VersionSupport(
            swwp=(1,),
            job_payloads={
                JobType.TTS_SYNTHESIZE: (1,),
                JobType.CONFIG_COMMIT: (1,),
            },
            job_results={
                JobType.TTS_SYNTHESIZE: (1,),
                JobType.CONFIG_COMMIT: (1,),
            },
            diagnostics=(1,),
            capability_manifest=(1,),
            configuration_schema=(1,),
        ),
    )


def principal(**updates) -> AuthenticatedPrincipal:
    values = {
        "principal_id": "principal_00000001",
        "worker_id": "worker_00000001",
        "enabled": True,
        "revoked": False,
        "expires_at": None,
        "queues": frozenset({QueueClass.ROUTINE, QueueClass.CONTROL}),
        "job_types": frozenset({JobType.TTS_SYNTHESIZE, JobType.CONFIG_COMMIT}),
        "capabilities": frozenset({"tts.synthesis.v1"}),
    }
    values.update(updates)
    return AuthenticatedPrincipal(**values)


@dataclass
class FakeDurable:
    clock: SimulatedClock
    assignment: JobAssignmentPayload | None = None
    acquired: int = 0
    acknowledged: int = 0
    renewed: int = 0
    progresses: list[JobProgress] = field(default_factory=list)
    results: list[JobResult] = field(default_factory=list)
    failures: int = 0
    result_error: Exception | None = None
    cancellations: int = 0

    def acquire(self, **_: object) -> JobAssignmentPayload | None:
        self.acquired += 1
        result = self.assignment
        self.assignment = None
        return result

    def acknowledge(self, lease: LeaseRef):
        assert lease == LEASE
        self.acknowledged += 1
        return object()

    def reject_unacknowledged(self, lease: LeaseRef):
        assert lease == LEASE
        return object()

    def renew(self, lease: LeaseRef):
        assert lease == LEASE
        self.renewed += 1
        return object()

    def progress(self, progress: JobProgress) -> None:
        self.progresses.append(progress)

    def result(self, result: JobResult) -> ResultCommitReceipt:
        self.results.append(result)
        if self.result_error is not None:
            raise self.result_error
        return ResultCommitReceipt(
            result.lease.job_id,
            result.lease.attempt,
            "0123456789abcdef",
            self.clock(),
            len(self.results) > 1,
        )

    def failure(self, failure):
        self.failures += 1
        return object()

    def request_cancellation(self, job_id: str):
        self.cancellations += 1
        return object()

    def reconcile(self, item: ReconcileItem) -> ReconcileDecision:
        from seasonalweather.swwp.constants import ReconcileDisposition

        return ReconcileDecision(
            lease=item.lease,
            disposition=ReconcileDisposition.RESUME,
            summary="durable attempt matches",
        )

    def reconcile_repository(self) -> None:
        return None


def assignment() -> JobAssignmentPayload:
    return JobAssignmentPayload(
        lease=LEASE,
        deadline_at=NOW + dt.timedelta(minutes=3),
        lease_expires_at=NOW + dt.timedelta(minutes=1),
        acknowledgment_deadline_at=NOW + dt.timedelta(seconds=10),
        job_type=JobType.TTS_SYNTHESIZE,
        queue=QueueClass.ROUTINE,
        executor=ExecutorClass.ROUTINE_WORKER,
        payload_schema_version=1,
        result_schema_version=1,
        configuration_generation=7,
        payload={
            "content_ref": "content_00000001",
            "voice_profile_ref": "profile_00000001",
            "output_format": "wav",
            "config_generation": 7,
        },
        capability_requirements=("tts.synthesis.v1",),
    )


def peers(*, auth: AuthenticatedPrincipal | None = None, accept: bool = True):
    clock = SimulatedClock(NOW)
    ids = DeterministicIds()
    durable = FakeDurable(clock=clock, assignment=assignment())
    controller = ControllerSession(
        controller_epoch=1,
        offered_subprotocols=("seasonalweather.worker.v1",),
        policy=StaticRegistrationPolicy(auth if auth is not None else principal()),
        durable=durable,
        id_factory=ids,
        clock=clock,
    )
    worker = WorkerSession(
        registration=registration(),
        id_factory=ids,
        clock=clock,
        accept_assignments=accept,
    )
    return SimulatedPeers(controller, worker), clock, durable


def test_registration_authorization_intersection_and_send_is_not_acceptance() -> None:
    simulation, _, durable = peers()
    simulation.start()
    simulation.pump()

    assert simulation.controller.state is ControllerState.ACTIVE
    assert simulation.worker.state is WorkerState.ACTIVE
    assert simulation.controller.accepted_queues == (QueueClass.ROUTINE,)
    assert simulation.controller.authorized_job_types == (JobType.TTS_SYNTHESIZE,)
    assert simulation.controller.authorized_capabilities == ("tts.synthesis.v1",)

    frame = simulation.deliver_assignment()
    assert frame is not None
    assert durable.acquired == 1
    assert durable.acknowledged == 0
    simulation.pump()
    assert durable.acknowledged == 1


@pytest.mark.parametrize(
    "auth",
    (
        None,
        principal(enabled=False),
        principal(revoked=True),
        principal(expires_at=NOW),
        principal(worker_id="worker_other_0001"),
    ),
)
def test_unauthenticated_disabled_revoked_expired_and_identity_mismatch_reject(auth) -> None:
    clock = SimulatedClock(NOW)
    ids = DeterministicIds()
    controller = ControllerSession(
        controller_epoch=1,
        offered_subprotocols=("seasonalweather.worker.v1",),
        policy=StaticRegistrationPolicy(auth),
        durable=FakeDurable(clock),
        id_factory=ids,
        clock=clock,
    )
    worker = WorkerSession(registration=registration(), id_factory=ids, clock=clock)
    response = controller.receive(worker.connect())

    assert controller.state is ControllerState.REJECTED
    assert isinstance(response[0].payload, RegistrationRejected)
    assert controller.session_id is None


def test_assignment_rejection_is_not_handler_failure_and_duplicate_acceptance_is_idempotent() -> None:
    simulation, _, durable = peers(accept=False)
    simulation.start()
    simulation.pump()
    simulation.deliver_assignment()
    simulation.pump()
    assert durable.acknowledged == 0
    assert durable.failures == 0

    simulation, _, durable = peers()
    simulation.start()
    simulation.pump()
    simulation.deliver_assignment()
    simulation.transport.duplicate_to_worker()
    simulation.pump()
    assert durable.acknowledged == 1


def test_result_before_explicit_acceptance_cannot_commit() -> None:
    simulation, _, durable = peers()
    simulation.start()
    simulation.pump()
    simulation.deliver_assignment()
    premature = simulation.worker.envelope(
        JobResult(
            lease=LEASE,
            result_schema_version=1,
            result={
                "artifact_ref": "artifact_00000001",
                "content_identity": "content_00000001",
                "duration_seconds": 3.5,
            },
            completion_id="completion_00000001",
        ),
        session_id=simulation.worker.session_id,
        worker_id=simulation.worker.registration.worker_id,
        worker_instance_id=simulation.worker.registration.worker_instance_id,
        controller_epoch=simulation.worker.controller_epoch,
        worker_epoch=simulation.worker.registration.worker_epoch,
    )

    response = simulation.controller.receive(premature)

    assert isinstance(response[0].payload, ProtocolErrorPayload)
    assert durable.results == []
    assert simulation.controller.state is ControllerState.FAILED


def test_heartbeat_progress_and_result_commit_ack_are_durably_ordered() -> None:
    simulation, _, durable = peers()
    simulation.start()
    simulation.pump()
    simulation.deliver_assignment()
    simulation.pump()

    simulation.transport.to_controller(simulation.worker.heartbeat())
    simulation.transport.to_controller(simulation.worker.progress(LEASE, stage="rendering", numeric={"percent": 50}))
    result = simulation.worker.result(
        LEASE,
        result_schema_version=1,
        result={
            "artifact_ref": "artifact_00000001",
            "content_identity": "content_00000001",
            "duration_seconds": 3.5,
        },
        completion_id="completion_00000001",
        artifact_refs=("artifact_00000001",),
    )
    simulation.transport.to_controller(result)
    simulation.pump_once()
    simulation.pump_once()
    simulation.pump_once()

    assert durable.renewed == 1
    assert len(durable.progresses) == 1
    assert len(durable.results) == 1
    assert LEASE.job_id in result.payload.lease.job_id
    assert simulation.worker.completions

    simulation.pump()
    assert not simulation.worker.completions


def test_result_commit_rejection_becomes_retryable_and_nonfatal() -> None:
    simulation, _, durable = peers()
    durable.result_error = RuntimeError("staging claim failed")
    simulation.start()
    simulation.pump()
    simulation.deliver_assignment()
    simulation.pump()

    result = simulation.worker.result(
        LEASE,
        result_schema_version=1,
        result={"content_identity": "content_00000001"},
        completion_id="completion_00000001",
    )
    response = simulation.controller.receive(result)

    assert isinstance(response[0].payload, ProtocolErrorPayload)
    assert response[0].payload.fatal is False
    assert response[0].payload.correlated_message_id == result.message_id
    assert "staging claim failed" not in response[0].payload.summary
    assert durable.failures == 1
    assert simulation.controller.state is ControllerState.ACTIVE


def test_lost_result_commit_ack_resends_identically_and_conflicts_fail_closed() -> None:
    simulation, _, durable = peers()
    simulation.start()
    simulation.pump()
    simulation.deliver_assignment()
    simulation.pump()
    result = simulation.worker.result(
        LEASE,
        result_schema_version=1,
        result={
            "artifact_ref": "artifact_00000001",
            "content_identity": "content_00000001",
            "duration_seconds": 3.5,
        },
        completion_id="completion_00000001",
    )
    first = simulation.controller.receive(result)
    assert isinstance(first[0].payload, ResultCommitted)
    assert len(durable.results) == 1
    assert simulation.controller.receive(result) == first
    assert len(durable.results) == 1

    conflict = result.model_copy(
        update={"payload": result.payload.model_copy(update={"completion_id": "completion_conflict_0001"})}
    )
    response = simulation.controller.receive(conflict)
    assert isinstance(response[0].payload, ProtocolErrorPayload)
    assert response[0].payload.category is ProtocolErrorCategory.RATE_SEQUENCE
    assert simulation.controller.state is ControllerState.FAILED


def test_stale_session_transport_loss_restart_reconciliation_and_drain() -> None:
    simulation, clock, _ = peers()
    simulation.start()
    simulation.pump()
    stale = simulation.worker.heartbeat().model_copy(update={"session_id": "session_stale_0001"})
    response = simulation.controller.receive(stale)
    assert isinstance(response[0].payload, ProtocolErrorPayload)
    assert simulation.controller.state is ControllerState.FAILED

    simulation, clock, _ = peers()
    simulation.start()
    simulation.pump()
    simulation.deliver_assignment()
    simulation.pump()
    drain = simulation.controller.request_drain(
        deadline_at=clock() + dt.timedelta(seconds=30),
        reason="service_shutdown",
    )
    assert simulation.controller.assign_next() is None
    responses = simulation.worker.receive(drain)
    assert simulation.worker.state is WorkerState.DRAINING
    simulation.controller.receive(responses[0])
    assert simulation.controller.state is ControllerState.DRAINING

    simulation.disconnect()
    assert simulation.worker.state is WorkerState.DISCONNECTED
    assert simulation.controller.state is ControllerState.CLOSED


def test_reconnect_uses_new_session_and_distinguishes_worker_restart() -> None:
    simulation, clock, durable = peers()
    simulation.start()
    simulation.pump()
    simulation.deliver_assignment()
    simulation.pump()
    prior_session = simulation.worker.session_id
    prior_epoch = simulation.worker.controller_epoch
    simulation.disconnect()

    ids = DeterministicIds()
    restarted_controller = ControllerSession(
        controller_epoch=2,
        offered_subprotocols=("seasonalweather.worker.v1",),
        policy=StaticRegistrationPolicy(principal()),
        durable=durable,
        id_factory=ids,
        clock=clock,
    )
    registered = restarted_controller.receive(simulation.worker.connect())
    simulation.worker.receive(registered[0])
    assert simulation.worker.session_id != prior_session
    assert simulation.worker.controller_epoch == 2
    report = simulation.worker.reconnect_report(
        prior_session_id=prior_session,
        prior_controller_epoch=prior_epoch,
    )
    reconciled = restarted_controller.receive(report)
    simulation.worker.receive(reconciled[0])
    assert simulation.worker.state is WorkerState.ACTIVE

    restarted_registration = registration().model_copy(
        update={"worker_instance_id": "instance_00000002", "worker_epoch": 2}
    )
    restarted_worker = WorkerSession(
        registration=restarted_registration,
        id_factory=ids,
        clock=clock,
    )
    next_controller = ControllerSession(
        controller_epoch=2,
        offered_subprotocols=("seasonalweather.worker.v1",),
        policy=StaticRegistrationPolicy(principal()),
        durable=durable,
        id_factory=ids,
        clock=clock,
    )
    response = next_controller.receive(restarted_worker.connect())
    restarted_worker.receive(response[0])
    assert restarted_worker.state is WorkerState.ACTIVE
    assert restarted_worker.registration.worker_epoch == 2
    assert restarted_worker.assignments == {}


def test_heartbeat_timeout_does_not_invent_job_outcome() -> None:
    simulation, clock, durable = peers()
    simulation.start()
    simulation.pump()
    simulation.transport.to_controller(simulation.worker.heartbeat())
    simulation.pump()
    clock.advance(46)

    assert simulation.controller.timed_out()
    assert simulation.controller.state is ControllerState.CLOSED
    assert durable.results == []
    assert durable.failures == 0


def test_cancel_is_durable_before_send_and_ack_is_not_completion() -> None:
    simulation, clock, durable = peers()
    simulation.start()
    simulation.pump()
    simulation.deliver_assignment()
    simulation.pump()

    cancel = simulation.controller.request_cancel(
        LEASE,
        deadline_at=clock() + dt.timedelta(seconds=5),
        reason="operator_request",
    )
    assert durable.cancellations == 1
    response = simulation.worker.receive(cancel)
    assert LEASE.job_id in {item[0] for item in simulation.worker.cancelled}
    assert simulation.controller.receive(response[0]) == ()
    assert durable.results == []
    assert durable.failures == 0


def test_simulated_transport_drop_duplicate_reorder_disconnect_and_bounds() -> None:
    simulation, _, _ = peers()
    simulation.start()
    simulation.transport.duplicate_to_controller()
    simulation.transport.reorder_controller()
    dropped = simulation.transport.drop_controller()
    assert dropped.payload.message_type == "register"
    simulation.pump()
    assert simulation.controller.state is ControllerState.ACTIVE
    simulation.disconnect()
    simulation.transport.to_controller(dropped)
    assert not simulation.transport.controller_inbox

    simulation.transport.maximum_frames = 1
    simulation.transport.reconnect()
    simulation.transport.to_controller(dropped)
    with pytest.raises(OverflowError):
        simulation.transport.to_controller(dropped)


def test_real_p1_07_adapter_leases_before_delivery_and_commits_before_ack(tmp_path) -> None:
    clock = SimulatedClock(NOW)
    lifecycle = Lifecycle()
    lifecycle.mark_running()
    repository = JobRepository(
        JobDatabase(path=str(tmp_path / "jobs.sqlite3"), busy_timeout_ms=1000),
        payload_max_bytes=4096,
        result_max_bytes=4096,
    )
    service = DurableJobService(
        repository,
        lifecycle,
        reconciliation_batch_size=20,
        clock=clock,
    )
    service.initialize()
    admitted = service.admit(
        job_type=JobType.TTS_SYNTHESIZE,
        payload={
            "content_ref": "content_00000001",
            "voice_profile_ref": "profile_00000001",
            "output_format": "wav",
            "config_generation": 7,
        },
        dedupe_key="tts:content_00000001",
        config_generation=7,
    )
    adapter = JobStoreSwwpAdapter(
        JobScheduler(
            repository,
            lifecycle,
            lease_seconds=60,
            acknowledgment_seconds=10,
            clock=clock,
        ),
        repository,
    )
    ids = DeterministicIds()
    controller = ControllerSession(
        controller_epoch=1,
        offered_subprotocols=("seasonalweather.worker.v1",),
        policy=StaticRegistrationPolicy(principal()),
        durable=adapter,
        id_factory=ids,
        clock=clock,
    )
    worker = WorkerSession(registration=registration(), id_factory=ids, clock=clock)
    simulation = SimulatedPeers(controller, worker)
    simulation.start()
    simulation.pump()

    missed = simulation.deliver_assignment()
    assert missed is not None
    assert repository.get(admitted.job.job_id).status is JobStatus.LEASED
    simulation.transport.drop_worker()
    clock.advance(11)
    controller.reconcile_missed_acknowledgments()
    assert repository.get(admitted.job.job_id).status is JobStatus.PENDING

    assigned = simulation.deliver_assignment()
    assert assigned is not None
    assert assigned.payload.lease.attempt == 2
    simulation.pump()
    assert repository.get(admitted.job.job_id).status is JobStatus.RUNNING

    result = worker.result(
        assigned.payload.lease,
        result_schema_version=1,
        result={
            "artifact_ref": "artifact_00000001",
            "content_identity": "content_00000001",
            "duration_seconds": 3.5,
        },
        completion_id="completion_00000001",
    )
    committed = controller.receive(result)
    assert repository.get(admitted.job.job_id).status is JobStatus.SUCCEEDED
    assert isinstance(committed[0].payload, ResultCommitted)
    assert worker.completions
    worker.receive(committed[0])
    assert not worker.completions

    failed_admission = service.admit(
        job_type=JobType.TTS_SYNTHESIZE,
        payload={
            "content_ref": "content_00000002",
            "voice_profile_ref": "profile_00000001",
            "output_format": "wav",
            "config_generation": 7,
        },
        dedupe_key="tts:content_00000002",
        config_generation=7,
    )
    failed_assignment = simulation.deliver_assignment()
    assert failed_assignment is not None
    simulation.pump()
    failed = worker.failure(
        failed_assignment.payload.lease,
        outcome=AttemptOutcome.PERMANENT_FAILURE,
        category=FailureCategory.INVALID_INPUT,
        error_code="invalid_input",
        summary="bounded invalid input",
    )
    assert controller.receive(failed) == ()
    assert repository.get(failed_admission.job.job_id).status is JobStatus.FAILED
