from __future__ import annotations

import datetime as dt

from seasonalweather.capabilities.hysteresis import (
    CapabilityHysteresis,
    CapabilityObservation,
    HysteresisPolicy,
)
from seasonalweather.capabilities.models import OperationalState
from seasonalweather.capabilities.registry import CapabilityRegistry
from seasonalweather.capabilities.service import (
    CapabilitySchedulerService,
    declared_capability_names,
)
from seasonalweather.jobs.policies import JobType, QueueClass
from seasonalweather.swwp.auth import AuthenticatedPrincipal, StaticRegistrationPolicy
from seasonalweather.swwp.capability_adapter import record_from_wire
from seasonalweather.swwp.constants import ControllerState, WorkerState
from seasonalweather.swwp.controller import ControllerSession
from seasonalweather.swwp.messages import (
    CapabilityOperationalState,
    Register,
    Registered,
    VersionSupport,
)
from seasonalweather.swwp.worker import WorkerSession
from tests.support.capabilities import wire_manifest, wire_record
from tests.support.swwp_simulation import (
    DeterministicIds,
    SimulatedCapabilityObserver,
    SimulatedClock,
    SimulatedPeers,
)

NOW = dt.datetime(2026, 7, 24, 12, tzinfo=dt.UTC)


class UnusedDurableAdapter:
    pass


def registration(clock: SimulatedClock) -> Register:
    return Register(
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        worker_epoch=1,
        software_version="0.17.0",
        build_identity="build_34f24f3",
        requested_queues=(QueueClass.ROUTINE,),
        requested_slots=1,
        capability_manifest=wire_manifest(
            (
                wire_record(
                    "tts.synthesis.v1",
                    now=clock(),
                    total=1,
                    available=1,
                    parameters={"format": "wav"},
                ),
            )
        ),
        supported_versions=VersionSupport(
            swwp=(1,),
            job_payloads={JobType.TTS_SYNTHESIZE: (1,)},
            job_results={JobType.TTS_SYNTHESIZE: (1,)},
            diagnostics=(1,),
            capability_manifest=(1,),
            configuration_schema=(1,),
        ),
    )


def policy() -> StaticRegistrationPolicy:
    return StaticRegistrationPolicy(
        AuthenticatedPrincipal(
            principal_id="principal_00000001",
            worker_id="worker_00000001",
            enabled=True,
            revoked=False,
            expires_at=None,
            queues=frozenset({QueueClass.ROUTINE}),
            job_types=frozenset({JobType.TTS_SYNTHESIZE}),
            capabilities=frozenset({"tts.synthesis.v1"}),
        )
    )


def simulated_runtime():
    clock = SimulatedClock(NOW)
    registry = CapabilityRegistry(
        allowed_capabilities=declared_capability_names(),
        maximum_validity_seconds=60,
    )
    service = CapabilitySchedulerService(
        registry,
        UnusedDurableAdapter(),  # type: ignore[arg-type]
        clock=clock,
        id_factory=DeterministicIds(),
        probe_timeout_seconds=10,
    )
    worker = WorkerSession(
        registration=registration(clock),
        id_factory=DeterministicIds(),
        clock=clock,
    )
    controller = ControllerSession(
        controller_epoch=1,
        offered_subprotocols=("seasonalweather.worker.v1",),
        policy=policy(),
        durable=service,
        capabilities=service,
        id_factory=DeterministicIds(),
        clock=clock,
    )
    peers = SimulatedPeers(controller, worker)
    peers.start()
    peers.pump()
    return clock, registry, service, peers


def test_registered_effective_capabilities_are_currently_schedulable_only() -> None:
    clock = SimulatedClock(NOW)
    registry = CapabilityRegistry(
        allowed_capabilities=declared_capability_names(),
        maximum_validity_seconds=60,
    )
    service = CapabilitySchedulerService(
        registry,
        UnusedDurableAdapter(),  # type: ignore[arg-type]
        clock=clock,
        id_factory=DeterministicIds(),
    )
    initial = registration(clock).model_copy(
        update={
            "capability_manifest": wire_manifest(
                (
                    wire_record(
                        "tts.synthesis.v1",
                        now=clock(),
                        state=CapabilityOperationalState.UNAVAILABLE,
                        accepting=False,
                        total=1,
                        available=0,
                        parameters={"format": "wav"},
                    ),
                    wire_record(
                        "cycle.regenerate.v1",
                        now=clock(),
                        total=1,
                        available=1,
                    ),
                )
            )
        }
    )
    worker = WorkerSession(
        registration=initial,
        id_factory=DeterministicIds(),
        clock=clock,
    )
    controller = ControllerSession(
        controller_epoch=1,
        offered_subprotocols=("seasonalweather.worker.v1",),
        policy=policy(),
        durable=service,
        capabilities=service,
        id_factory=DeterministicIds(),
        clock=clock,
    )

    responses = controller.receive(worker.connect())

    assert isinstance(responses[0].payload, Registered)
    assert responses[0].payload.authorized_capabilities == ("tts.synthesis.v1",)
    assert responses[0].payload.effective_capabilities == ()


def test_simulation_updates_duplicates_gap_probe_stale_and_reconnect() -> None:
    clock, registry, service, peers = simulated_runtime()
    worker = peers.worker
    initial = registry.snapshot("worker_00000001", clock())

    assert worker.state is WorkerState.ACTIVE
    assert initial is not None
    assert initial.trusted is True

    degraded = wire_record(
        "tts.synthesis.v1",
        now=clock(),
        state=CapabilityOperationalState.DEGRADED,
        accepting=False,
        total=1,
        available=0,
        parameters={"format": "wav"},
    )
    update = worker.capability_update(
        changed=(degraded,),
        validity_seconds=60,
    )
    peers.transport.to_controller(update)
    peers.transport.to_controller(update)
    peers.pump()
    after_duplicate = registry.snapshot("worker_00000001", clock())
    assert after_duplicate is not None
    assert after_duplicate.epoch == 2
    assert after_duplicate.effective_capacity["tts.synthesis.v1"] == 0

    healthy = wire_record(
        "tts.synthesis.v1",
        now=clock(),
        total=1,
        available=1,
        parameters={"format": "wav"},
    )
    worker.capability_update(changed=(healthy,), validity_seconds=60)
    gapped = worker.capability_update(changed=(healthy,), validity_seconds=60)
    peers.transport.to_controller(gapped)
    peers.pump()
    recovered = registry.snapshot("worker_00000001", clock())
    assert recovered is not None
    assert recovered.epoch == worker.capability_manifest.epoch
    assert recovered.trusted is True
    assert recovered.probe_required is False

    valid = worker.capability_update(changed=(healthy,), validity_seconds=60)
    corrupt_payload = valid.payload.model_copy(update={"full_digest": "sha256:" + "f" * 64})
    corrupt = valid.model_copy(update={"payload": corrupt_payload})
    peers.transport.to_controller(corrupt)
    peers.pump()
    after_corruption = registry.snapshot("worker_00000001", clock())
    assert after_corruption is not None
    assert after_corruption.trusted is True
    assert after_corruption.epoch == worker.capability_manifest.epoch

    clock.advance(61)
    assert service.tick() == ("worker_00000001",)
    peers.transport.to_worker(peers.controller._capability_probe_frames()[0])
    peers.pump()
    refreshed = registry.snapshot("worker_00000001", clock())
    assert refreshed is not None
    assert refreshed.trusted is True

    prior_session = worker.session_id
    peers.disconnect()
    controller = ControllerSession(
        controller_epoch=2,
        offered_subprotocols=("seasonalweather.worker.v1",),
        policy=policy(),
        durable=service,
        capabilities=service,
        id_factory=peers.controller.id_factory,
        clock=clock,
    )
    reconnected = SimulatedPeers(controller, worker)
    reconnected.start()
    reconnected.pump()
    current = registry.snapshot("worker_00000001", clock())

    assert controller.state is ControllerState.ACTIVE
    assert worker.state is WorkerState.ACTIVE
    assert worker.session_id != prior_session
    assert current is not None
    assert current.trusted is True
    assert current.probe_required is False


def test_simulation_reordered_update_for_old_session_cannot_mutate_registry() -> None:
    clock, registry, service, peers = simulated_runtime()
    stale_update = peers.worker.capability_update(
        changed=(
            wire_record(
                "tts.synthesis.v1",
                now=clock(),
                state=CapabilityOperationalState.DISABLED,
                accepting=False,
                total=1,
                available=0,
                parameters={"format": "wav"},
            ),
        ),
        validity_seconds=60,
    )
    peers.disconnect()
    controller = ControllerSession(
        controller_epoch=2,
        offered_subprotocols=("seasonalweather.worker.v1",),
        policy=policy(),
        durable=service,
        capabilities=service,
        id_factory=peers.controller.id_factory,
        clock=clock,
    )
    current = SimulatedPeers(controller, peers.worker)
    current.start()
    current.pump()
    before = registry.snapshot("worker_00000001", clock())

    current.transport.to_controller(stale_update)
    current.pump()
    after = registry.snapshot("worker_00000001", clock())

    assert before is not None and after is not None
    assert after.digest == before.digest
    assert after.epoch == before.epoch


def test_simulated_hysteresis_observations_prevent_flapping() -> None:
    clock, registry, _, peers = simulated_runtime()
    machine = CapabilityHysteresis(
        record_from_wire(peers.worker.capability_manifest.records[0]),
        policy=HysteresisPolicy(
            failure_threshold=2,
            recovery_success_threshold=2,
        ),
        clock=clock,
    )
    observer = SimulatedCapabilityObserver(
        peers.worker,
        {"tts.synthesis.v1": machine},
    )

    assert (
        observer.observe(
            "tts.synthesis.v1",
            CapabilityObservation(successful=False),
        )
        is None
    )
    degraded = observer.observe(
        "tts.synthesis.v1",
        CapabilityObservation(
            successful=False,
            state=OperationalState.DEGRADED,
            accepting_new_jobs=False,
            available_capacity=0,
        ),
    )
    assert degraded is not None
    peers.transport.to_controller(degraded)
    peers.pump()
    assert (
        registry.snapshot(
            "worker_00000001",
            clock(),
        ).effective_capacity["tts.synthesis.v1"]
        == 0
    )

    assert (
        observer.observe(
            "tts.synthesis.v1",
            CapabilityObservation(successful=True),
        )
        is None
    )
    recovered = observer.observe(
        "tts.synthesis.v1",
        CapabilityObservation(
            successful=True,
            state=OperationalState.HEALTHY,
            accepting_new_jobs=True,
            available_capacity=1,
        ),
    )
    assert recovered is not None
    peers.transport.to_controller(recovered)
    peers.pump()
    assert (
        registry.snapshot(
            "worker_00000001",
            clock(),
        ).effective_capacity["tts.synthesis.v1"]
        == 1
    )
