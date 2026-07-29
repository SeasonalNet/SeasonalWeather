from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from seasonalweather.database import SeasonalDatabase
from seasonalweather.diagnostics.bindings import RUNTIME_CODES
from seasonalweather.jobs.policies import JobType, QueueClass
from seasonalweather.runtime_diagnostics.repository import OccurrenceRepository
from seasonalweather.runtime_diagnostics.service import RuntimeDiagnosticService
from seasonalweather.runtime_diagnostics.worker import WorkerDiagnosticTranslator
from seasonalweather.swwp.auth import AuthenticatedPrincipal, StaticRegistrationPolicy
from seasonalweather.swwp.codec import decode, encode
from seasonalweather.swwp.constants import ControllerState, WorkerState
from seasonalweather.swwp.controller import ControllerSession
from seasonalweather.swwp.messages import (
    DiagnosticEvidence,
    DiagnosticFrame,
    DiagnosticTransition,
    Envelope,
    Register,
    VersionSupport,
    WorkerDiagnostic,
)
from seasonalweather.swwp.worker import WorkerSession
from tests.support.capabilities import wire_manifest, wire_record
from tests.support.swwp_simulation import DeterministicIds, SimulatedClock, SimulatedPeers

NOW = dt.datetime(2026, 7, 29, 12, tzinfo=dt.UTC)


def _translator(tmp_path: Path) -> tuple[WorkerDiagnosticTranslator, OccurrenceRepository]:
    database = SeasonalDatabase(path=str(tmp_path / "operational.sqlite3"))
    database.bootstrap()
    repository = OccurrenceRepository(database)
    service = RuntimeDiagnosticService(repository, clock=lambda: NOW)
    service.initialize()
    return WorkerDiagnosticTranslator(service, "controller_00000001"), repository


def _diagnostic(**updates: object) -> WorkerDiagnostic:
    values = {
        "envelope_schema_version": 1,
        "diagnostic_schema_version": 1,
        "catalog_version": 1,
        "diagnostic_id": "diagnostic_00000001",
        "code": RUNTIME_CODES["optional_task_degraded"],
        "short_message": "Synthetic worker degradation.",
        "component": "worker-source",
        "fatal_hint": True,
        "retryable_hint": False,
    }
    values.update(updates)
    return WorkerDiagnostic(**values)  # type: ignore[arg-type]


def _handle(
    translator: WorkerDiagnosticTranslator,
    diagnostic: WorkerDiagnostic,
    *,
    worker: str = "worker_00000001",
    instance: str = "instance_00000001",
    session: str = "session_00000001",
    epoch: int = 1,
):
    return translator.handle(
        diagnostic,
        worker_id=worker,
        worker_instance_id=instance,
        session_id=session,
        worker_epoch=epoch,
    )


def test_worker_diagnostic_codec_and_controller_policy_override(tmp_path: Path) -> None:
    diagnostic = _diagnostic()
    envelope = Envelope(
        message_type=diagnostic.message_type,
        message_id="message_00000001",
        sent_at=NOW,
        session_id="session_00000001",
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        controller_epoch=1,
        worker_epoch=1,
        payload=diagnostic,
    )
    decoded = decode(encode(envelope))
    assert decoded == envelope

    translator, repository = _translator(tmp_path)
    acknowledgment = translator.handle(
        diagnostic,
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        worker_epoch=1,
    )
    assert acknowledgment.accepted
    occurrence = repository.get(acknowledgment.controller_occurrence_id or "")
    assert occurrence is not None
    assert occurrence.latest_instance["fatal"] is False
    assert occurrence.latest_instance["retryable"] is True


def test_worker_catalog_mismatch_unknown_code_duplicate_and_resolution(tmp_path: Path) -> None:
    translator, repository = _translator(tmp_path)
    future = _diagnostic(catalog_version=2, code="SWTTS4001")
    first = translator.handle(
        future,
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        worker_epoch=1,
    )
    second = translator.handle(
        future,
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        worker_epoch=1,
    )
    assert first.compatibility and second.compatibility
    assert first.controller_occurrence_id == second.controller_occurrence_id
    occurrence = repository.get(first.controller_occurrence_id or "")
    assert occurrence is not None
    assert occurrence.code == RUNTIME_CODES["worker_diagnostic_incompatible"]
    assert "SWTTS4001" in occurrence.latest_instance["message"]

    resolution = _diagnostic(
        catalog_version=2,
        code="SWTTS4001",
        transition=DiagnosticTransition.RESOLVED,
        controller_occurrence_id=first.controller_occurrence_id,
        short_message="Synthetic worker recovered.",
    )
    resolved = translator.handle(
        resolution,
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        worker_epoch=1,
    )
    assert resolved.accepted
    assert repository.get(first.controller_occurrence_id or "").state.value == "resolved"  # type: ignore[union-attr]


def test_multi_worker_ownership_resolves_only_after_last_worker(tmp_path: Path) -> None:
    translator, repository = _translator(tmp_path)
    first = _handle(translator, _diagnostic(), worker="worker_00000001")
    second = _handle(
        translator,
        _diagnostic(),
        worker="worker_00000002",
        instance="instance_00000002",
        session="session_00000002",
    )
    assert first.controller_occurrence_id == second.controller_occurrence_id
    occurrence_id = first.controller_occurrence_id or ""

    first_resolution = _handle(
        translator,
        _diagnostic(
            transition=DiagnosticTransition.RESOLVED,
            controller_occurrence_id=occurrence_id,
            short_message="First worker recovered.",
        ),
        worker="worker_00000001",
    )
    assert first_resolution.accepted
    assert "another worker" in first_resolution.summary
    assert repository.get(occurrence_id).state.value == "active"  # type: ignore[union-attr]

    second_resolution = _handle(
        translator,
        _diagnostic(
            transition=DiagnosticTransition.RESOLVED,
            controller_occurrence_id=occurrence_id,
            short_message="Second worker recovered.",
        ),
        worker="worker_00000002",
        instance="instance_00000002",
        session="session_00000002",
    )
    assert second_resolution.accepted
    assert repository.get(occurrence_id).state.value == "resolved"  # type: ignore[union-attr]


def test_diagnostic_id_idempotency_contradiction_and_session_cleanup(tmp_path: Path) -> None:
    translator, repository = _translator(tmp_path)
    diagnostic = _diagnostic()
    first = _handle(translator, diagnostic)
    repeated = _handle(translator, diagnostic)
    assert repeated == first
    occurrence = repository.get(first.controller_occurrence_id or "")
    assert occurrence is not None
    assert occurrence.count == 1

    contradictory = _handle(
        translator,
        _diagnostic(short_message="Contradictory reuse."),
    )
    assert not contradictory.accepted
    assert "contradictory" in contradictory.summary
    assert any(
        item.code == RUNTIME_CODES["worker_diagnostic_rejected"]
        and item.latest_instance["context"]["reason_code"] == "contradictory_diagnostic_reuse"
        for item in repository.active()
    )

    translator.release_session(
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        worker_epoch=1,
    )
    assert translator.relationship_count == 0
    assert repository.get(first.controller_occurrence_id or "").state.value == "active"  # type: ignore[union-attr]


def test_worker_schema_mismatch_and_same_catalog_unknown_code(tmp_path: Path) -> None:
    translator, repository = _translator(tmp_path)
    mismatch = translator.handle(
        _diagnostic(diagnostic_schema_version=2),
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        worker_epoch=1,
    )
    assert mismatch.accepted and mismatch.compatibility

    unknown = translator.handle(
        _diagnostic(code="SWTTS4001", diagnostic_id="diagnostic_00000002"),
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        worker_epoch=1,
    )
    assert not unknown.accepted
    assert any(item.code == RUNTIME_CODES["worker_diagnostic_rejected"] for item in repository.active())


def test_worker_evidence_is_redacted_and_session_relationship_is_scoped(tmp_path: Path) -> None:
    translator, repository = _translator(tmp_path)
    active = translator.handle(
        _diagnostic(
            evidence=DiagnosticEvidence(
                exception_type="builtins.RuntimeError",
                message="password=synthetic-secret",
                notes=("token=synthetic-secret",),
                frames=(
                    DiagnosticFrame(
                        filename="/home/private/operator/project/worker.py",
                        line=10,
                        function="run",
                        source="raise RuntimeError('password=synthetic-secret')",
                    ),
                    DiagnosticFrame(
                        filename=r"C:\Users\Private\AppData\module.py",
                        line=11,
                        function="poll",
                    ),
                ),
            )
        ),
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        worker_epoch=1,
    )
    occurrence = repository.get(active.controller_occurrence_id or "")
    assert occurrence is not None
    assert "synthetic-secret" not in str(occurrence.latest_instance)
    frames = occurrence.latest_instance["exception_evidence"]["frames"]
    assert [frame["filename"] for frame in frames] == ["worker.py", "module.py"]
    assert "/home/private" not in str(occurrence.latest_instance)
    assert "C:\\Users" not in str(occurrence.latest_instance)

    cross_session_resolution = translator.handle(
        _diagnostic(
            transition=DiagnosticTransition.RESOLVED,
            controller_occurrence_id=active.controller_occurrence_id,
            short_message="Recovered.",
        ),
        worker_id="worker_00000001",
        worker_instance_id="instance_00000002",
        session_id="session_00000002",
        worker_epoch=2,
    )
    assert not cross_session_resolution.accepted
    assert repository.get(active.controller_occurrence_id or "").state.value == "active"  # type: ignore[union-attr]


@pytest.mark.parametrize("code", ("SWCACHE1001", "SWREDIS1001"))
@pytest.mark.parametrize("catalog_version", (1, 2))
def test_reserved_worker_namespaces_are_rejected_before_compatibility(
    tmp_path: Path,
    code: str,
    catalog_version: int,
) -> None:
    translator, repository = _translator(tmp_path)
    diagnostic = WorkerDiagnostic.model_construct(
        **{
            **_diagnostic().model_dump(mode="python"),
            "diagnostic_id": f"diagnostic_{code.lower()}_{catalog_version}",
            "code": code,
            "catalog_version": catalog_version,
        }
    )
    acknowledgment = _handle(translator, diagnostic)
    assert not acknowledgment.accepted
    assert not acknowledgment.compatibility
    assert acknowledgment.summary == "worker diagnostic code uses a reserved namespace"
    active = repository.active()
    assert any(
        item.code == RUNTIME_CODES["worker_diagnostic_rejected"]
        and item.latest_instance["context"]["reason_code"] == "reserved_diagnostic_namespace"
        for item in active
    )
    assert code not in str(active)


def test_worker_cannot_resolve_another_occurrence(tmp_path: Path) -> None:
    translator, repository = _translator(tmp_path)
    rejected = translator.handle(
        _diagnostic(
            transition=DiagnosticTransition.RESOLVED,
            controller_occurrence_id="occ_unauthorized0001",
        ),
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        worker_epoch=1,
    )
    assert not rejected.accepted
    assert any(item.code == RUNTIME_CODES["worker_diagnostic_rejected"] for item in repository.active())


def test_simulated_session_overrides_identity_and_replays_diagnostic(tmp_path: Path) -> None:
    translator, repository = _translator(tmp_path)
    clock = SimulatedClock(NOW)
    ids = DeterministicIds()
    registration = Register(
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        worker_epoch=1,
        software_version="0.18.0",
        build_identity="build_synthetic",
        requested_queues=(QueueClass.ROUTINE,),
        requested_slots=1,
        capability_manifest=wire_manifest((wire_record("tts.synthesis.v1", now=NOW),)),
        supported_versions=VersionSupport(
            swwp=(1,),
            diagnostics=(1,),
            capability_manifest=(1,),
            configuration_schema=(1,),
        ),
    )
    principal = AuthenticatedPrincipal(
        principal_id="principal_00000001",
        worker_id=registration.worker_id,
        enabled=True,
        revoked=False,
        expires_at=None,
        queues=frozenset({QueueClass.ROUTINE}),
        job_types=frozenset({JobType.TTS_SYNTHESIZE}),
        capabilities=frozenset({"tts.synthesis.v1"}),
    )

    class UnusedDurable:
        def __getattr__(self, name: str):
            raise AssertionError(f"durable port was unexpectedly used: {name}")

    controller = ControllerSession(
        controller_epoch=1,
        offered_subprotocols=("seasonalweather.worker.v1",),
        policy=StaticRegistrationPolicy(principal),
        durable=UnusedDurable(),  # type: ignore[arg-type]
        id_factory=ids,
        clock=clock,
        diagnostics=translator,
    )
    worker = WorkerSession(registration=registration, id_factory=ids, clock=clock)
    peers = SimulatedPeers(controller, worker)
    peers.start()
    peers.pump()
    frame = worker.diagnostic(_diagnostic())
    peers.transport.to_controller(frame)
    peers.pump()
    second_frame = worker.diagnostic(_diagnostic())
    assert second_frame.message_id != frame.message_id
    peers.transport.to_controller(second_frame)
    peers.pump()
    active = repository.active()
    assert len(active) == 1
    assert active[0].count == 1
    assert active[0].latest_instance["context"]["worker_id"] == registration.worker_id
    acknowledgment = worker.diagnostic_acknowledgments["diagnostic_00000001"]
    assert acknowledgment.accepted
    assert worker.diagnostic_occurrences["diagnostic_00000001"] == acknowledgment.controller_occurrence_id

    class UnavailableService:
        def build(self, **_values: object) -> object:
            raise RuntimeError("active limit or persistence unavailable")

        def promote(self, _instance: object) -> object:
            raise AssertionError("unreachable")

    translator.service = UnavailableService()  # type: ignore[assignment]
    rejected_id = "diagnostic_00000002"
    peers.transport.to_controller(worker.diagnostic(_diagnostic(diagnostic_id=rejected_id)))
    peers.pump()
    assert not worker.diagnostic_acknowledgments[rejected_id].accepted
    assert controller.state is ControllerState.ACTIVE
    assert worker.state is WorkerState.ACTIVE


def test_translator_and_controller_fail_closed_on_internal_rejection(tmp_path: Path) -> None:
    translator, _ = _translator(tmp_path)
    unsafe = WorkerDiagnostic.model_construct(
        **{
            **_diagnostic().model_dump(mode="python"),
            "diagnostic_id": "unsafe diagnostic identifier",
            "component": "token=unsafe-secret",
        }
    )
    unsafe_ack = _handle(translator, unsafe)
    assert not unsafe_ack.accepted

    class UnavailableService:
        def build(self, **_values: object) -> object:
            raise RuntimeError("active limit or persistence unavailable")

        def promote(self, _instance: object) -> object:
            raise AssertionError("unreachable")

    translator.service = UnavailableService()  # type: ignore[assignment]
    acknowledgment = _handle(translator, _diagnostic())
    assert not acknowledgment.accepted
    assert acknowledgment.summary == "worker diagnostic input was rejected safely"
