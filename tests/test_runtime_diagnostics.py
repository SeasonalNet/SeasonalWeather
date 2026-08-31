from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx2
import pytest

from seasonalweather.database import SeasonalDatabase
from seasonalweather.diagnostics.bindings import RUNTIME_CODES
from seasonalweather.runtime_diagnostics.evidence import capture_exception
from seasonalweather.runtime_diagnostics.fingerprint import Fingerprint, fingerprint
from seasonalweather.runtime_diagnostics.marker import (
    ProcessMarkerStore,
    controller_marker,
)
from seasonalweather.runtime_diagnostics.models import (
    CorrelationContext,
    DiagnosticRole,
    OccurrenceState,
    PromotionReason,
    ResolutionEvidence,
)
from seasonalweather.runtime_diagnostics.repository import (
    MAX_ACTIVE_OCCURRENCES,
    MAX_TRANSITIONS_PER_OCCURRENCE,
    OccurrenceRepository,
    RecordDisposition,
)
from seasonalweather.runtime_diagnostics.representations import (
    occurrence_detail,
    occurrence_summary,
)
from seasonalweather.runtime_diagnostics.service import RuntimeDiagnosticService
from seasonalweather.tts.adapters import OpenAICompatibleAdapter, OpenAICompatibleConfig
from seasonalweather.tts.failures import ProcessFailure
from seasonalweather.tts.models import BackendId, SynthesisPurpose, SynthesisRequest

NOW = dt.datetime(2026, 7, 29, 12, tzinfo=dt.UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += dt.timedelta(seconds=seconds)


def _service(tmp_path: Path, clock: Clock | None = None) -> RuntimeDiagnosticService:
    database = SeasonalDatabase(path=str(tmp_path / "operational.sqlite3"))
    database.bootstrap()
    service = RuntimeDiagnosticService(OccurrenceRepository(database), clock=clock)
    service.initialize()
    return service


def _context(**updates: object) -> CorrelationContext:
    values = {
        "role": DiagnosticRole.CONTROLLER,
        "instance_id": "controller_00000001",
        "component": "optional-source",
        "reason_code": "optional_task_failed",
    }
    values.update(updates)
    return CorrelationContext(**values)  # type: ignore[arg-type]


def _instance(
    service: RuntimeDiagnosticService,
    *,
    context: CorrelationContext | None = None,
    exception: BaseException | None = None,
):
    return service.build(
        code=RUNTIME_CODES["optional_task_degraded"],
        context=context or _context(),
        message="An optional supervised task failed.",
        operational_effect="The optional source is degraded.",
        recovery_action="Inspect evidence and recover the source.",
        promotion_reason=PromotionReason.DEGRADATION,
        exception=exception,
    )


def _raise_nested() -> None:
    def inner() -> None:
        error = RuntimeError("password=synthetic-secret")
        error.add_note("token=synthetic-secret")
        raise error

    inner()


def test_exception_evidence_preserves_frames_cause_context_notes_and_group() -> None:
    try:
        try:
            _raise_nested()
        except RuntimeError as cause:
            raise ValueError("outer") from cause
    except ValueError as explicit:
        explicit_evidence = capture_exception(explicit)

    assert explicit_evidence["type"].endswith("ValueError")
    assert explicit_evidence["cause"]["type"].endswith("RuntimeError")
    assert len(explicit_evidence["cause"]["frames"]) >= 2
    encoded = json.dumps(explicit_evidence)
    assert "synthetic-secret" not in encoded
    assert "[REDACTED]" in encoded
    assert "locals" not in encoded

    try:
        try:
            raise KeyError("context")
        except KeyError:
            raise LookupError("implicit")  # noqa: B904 - exercises implicit context
    except LookupError as implicit:
        assert capture_exception(implicit)["context"]["type"].endswith("KeyError")

    try:
        try:
            raise KeyError("hidden")
        except KeyError:
            raise LookupError("suppressed") from None
    except LookupError as suppressed:
        assert "context" not in capture_exception(suppressed)

    group = ExceptionGroup("group", [ValueError("one"), ExceptionGroup("nested", [TypeError("two")])])
    group_evidence = capture_exception(group)
    assert group_evidence["members"][1]["members"][0]["type"].endswith("TypeError")


def test_remote_failure_runtime_evidence_excludes_provider_and_secret_sentinels(tmp_path: Path) -> None:
    sentinels = (
        "API-KEY-SENTINEL",
        "SEASONAL-CLIENT-CREDENTIAL",
        "SEASONAL-ACCESS-TOKEN",
        "Authorization: Bearer SECRET-AUTH-VALUE",
        "raw synthesis text sentinel",
        "arbitrary provider transport detail",
    )
    key = tmp_path / "api-key"
    key.write_text("API-KEY-SENTINEL", encoding="ascii")

    class Transport:
        async def request(self, method, url, *, headers, json, timeout):
            del method, url, headers, json, timeout
            raise httpx2.ConnectError("; ".join(sentinels))

        async def close(self):
            pass

    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="model",
            voice="alloy",
        ),
        transport=Transport(),
    )
    request = SynthesisRequest(
        purpose=SynthesisPurpose.ROUTINE,
        backend=BackendId.OPENAI_COMPATIBLE,
        text="raw synthesis text sentinel",
        deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5),
    )
    with pytest.raises(ProcessFailure) as raised:
        adapter.synthesize(
            request,
            request.text,
            output_dir=tmp_path,
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    evidence = capture_exception(raised.value)
    encoded = json.dumps(evidence, sort_keys=True)
    assert all(sentinel not in encoded for sentinel in sentinels)


@pytest.mark.parametrize("cleanup_stage", ["response", "transport", "cancel", "deadline"])
def test_remote_cleanup_failures_are_redacted_in_real_runtime_evidence(tmp_path: Path, cleanup_stage: str) -> None:
    sentinels = (
        "API-KEY-CLEANUP-SENTINEL",
        "SEASONAL-CLIENT-CREDENTIAL-CLEANUP",
        "SEASONAL-ACCESS-TOKEN-CLEANUP",
        "Authorization: Bearer CLEANUP-AUTH-VALUE",
        "raw cleanup synthesis text",
        "arbitrary cleanup provider detail",
    )
    key = tmp_path / "api-key"
    key.write_text(sentinels[0], encoding="ascii")

    class Response:
        status_code = 200
        headers = {"content-type": "audio/wav", "content-length": "4"}

        async def aiter_bytes(self, chunk_size: int = 65_536):
            del chunk_size
            yield b"RIFF"

        async def aclose(self):
            if cleanup_stage == "response":
                raise RuntimeError("response.close " + " ".join(sentinels))

    class Transport:
        def __init__(self):
            self.release = asyncio.Event()
            self.entered = threading.Event()

        def for_operation(self):
            return self

        async def request(self, method, url, *, headers, json, timeout):
            del method, url, headers, json, timeout
            if cleanup_stage in {"cancel", "deadline"}:
                self.entered.set()
                await self.release.wait()
            return Response()

        async def close(self):
            if cleanup_stage in {"cancel", "deadline"}:
                self.release.set()
            if cleanup_stage == "transport":
                raise RuntimeError("transport.close " + " ".join(sentinels))

    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="model",
            voice="alloy",
        ),
        transport=Transport(),
    )
    request = SynthesisRequest(
        purpose=SynthesisPurpose.ROUTINE,
        backend=BackendId.OPENAI_COMPATIBLE,
        text=sentinels[4],
        deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5),
    )
    cancellation = threading.Event()
    if cleanup_stage == "cancel":
        failures: list[ProcessFailure] = []

        def run() -> None:
            try:
                adapter.synthesize(
                    request,
                    request.text,
                    output_dir=tmp_path,
                    deadline=time.monotonic() + 1,
                    cancellation=cancellation,
                )
            except ProcessFailure as failure:
                failures.append(failure)

        worker = threading.Thread(target=run)
        worker.start()
        assert adapter._transport is not None
        assert adapter._transport.entered.wait(1)
        cancellation.set()
        worker.join(1)
        assert not worker.is_alive() and failures
        evidence = capture_exception(failures[0])
    else:
        try:
            adapter.synthesize(
                request,
                request.text,
                output_dir=tmp_path,
                deadline=time.monotonic() + (0.05 if cleanup_stage == "deadline" else 1),
                cancellation=cancellation,
            )
        except ProcessFailure as failure:
            evidence = capture_exception(failure)
        else:
            raise AssertionError("cleanup scenario unexpectedly succeeded")
    encoded = json.dumps(evidence, sort_keys=True)
    assert all(sentinel not in encoded for sentinel in sentinels)


def test_exception_evidence_records_explicit_cycle_and_truncation() -> None:
    cyclic = RuntimeError("cycle")
    cyclic.__cause__ = cyclic
    cycle_evidence = capture_exception(cyclic)
    assert cycle_evidence["cause"]["truncated"]["cycle"] is True

    group = ExceptionGroup("wide", [ValueError(str(index)) for index in range(20)])
    group_evidence = capture_exception(group)
    assert len(group_evidence["members"]) == 16
    assert group_evidence["truncated"]["group_members"] == 4


def test_models_reject_unbounded_context_and_representations_are_deterministic(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="component"):
        _context(component="x" * 65)
    with pytest.raises(ValueError, match="secret"):
        _context(worker_id="token:synthetic-secret")

    service = _service(tmp_path, Clock())
    try:
        _raise_nested()
    except RuntimeError as error:
        instance = _instance(service, exception=error)
    result = service.promote(instance)
    assert "synthetic-secret" not in repr(instance)
    assert "synthetic-secret" not in fingerprint(instance).canonical_key
    summary = occurrence_summary(result.occurrence)
    detail = occurrence_detail(result.occurrence)
    assert summary == occurrence_summary(result.occurrence)
    assert detail == occurrence_detail(result.occurrence)
    assert json.dumps(detail, sort_keys=True, allow_nan=False)


def test_runtime_and_occurrence_evidence_are_deeply_immutable(tmp_path: Path) -> None:
    service = _service(tmp_path, Clock())
    source = {
        "type": "builtins.RuntimeError",
        "message": "bounded failure",
        "frames": [{"filename": "worker.py", "line": 4, "function": "run"}],
    }
    instance = service.build(
        code=RUNTIME_CODES["optional_task_degraded"],
        context=_context(),
        message="An optional supervised task failed.",
        operational_effect="The optional source is degraded.",
        recovery_action="Inspect evidence and recover the source.",
        promotion_reason=PromotionReason.DEGRADATION,
        exception_evidence=source,
    )
    source["message"] = "mutated"
    source["frames"][0]["filename"] = "mutated.py"  # type: ignore[index]
    assert instance.to_dict()["exception_evidence"]["message"] == "bounded failure"  # type: ignore[index]
    assert instance.to_dict()["exception_evidence"]["frames"][0]["filename"] == "worker.py"  # type: ignore[index]
    with pytest.raises(TypeError):
        instance.exception_evidence["message"] = "mutated"  # type: ignore[index,union-attr]

    result = service.promote(instance)
    exported = result.occurrence.latest_instance
    with pytest.raises(TypeError):
        exported["message"] = "mutated"  # type: ignore[index]
    detail = occurrence_detail(result.occurrence)
    detail["latest_instance"]["message"] = "detached mutation"
    assert result.occurrence.latest_instance["message"] != "detached mutation"


def test_frozen_exception_evidence_remains_fingerprint_material(tmp_path: Path) -> None:
    service = _service(tmp_path, Clock())

    def build(exception_type: str, filename: str):
        return service.build(
            code=RUNTIME_CODES["optional_task_degraded"],
            context=_context(),
            message="An optional supervised task failed.",
            operational_effect="The optional source is degraded.",
            recovery_action="Inspect evidence and recover the source.",
            promotion_reason=PromotionReason.DEGRADATION,
            exception_evidence={
                "type": exception_type,
                "message": "bounded failure",
                "frames": [
                    {
                        "filename": filename,
                        "line": 41,
                        "function": "run",
                    }
                ],
            },
        )

    original = build("builtins.RuntimeError", "source_a.py")
    identical = build("builtins.RuntimeError", "source_a.py")
    different_type = build("builtins.ValueError", "source_a.py")
    different_frame = build("builtins.RuntimeError", "source_b.py")
    original_fingerprint = fingerprint(original)
    identical_fingerprint = fingerprint(identical)
    type_fingerprint = fingerprint(different_type)
    frame_fingerprint = fingerprint(different_frame)

    assert identical_fingerprint == original_fingerprint
    assert type_fingerprint.canonical_key != original_fingerprint.canonical_key
    assert type_fingerprint.digest != original_fingerprint.digest
    assert frame_fingerprint.canonical_key != original_fingerprint.canonical_key
    assert frame_fingerprint.digest != original_fingerprint.digest
    assert json.loads(original_fingerprint.canonical_key)["exception_type"] == "builtins.RuntimeError"
    assert json.loads(original_fingerprint.canonical_key)["top_frame"] == {
        "filename": "source_a.py",
        "function": "run",
        "line": 41,
    }

    created = service.promote(original)
    repeated = service.promote(identical)
    assert repeated.disposition is RecordDisposition.REPEATED
    assert repeated.occurrence.occurrence_id == created.occurrence.occurrence_id
    assert repeated.occurrence.count == 2
    assert service.promote(different_type).occurrence.occurrence_id != created.occurrence.occurrence_id
    assert service.promote(different_frame).occurrence.occurrence_id != created.occurrence.occurrence_id


def test_resolution_evidence_is_typed_redacted_and_complete(tmp_path: Path) -> None:
    service = _service(tmp_path, Clock())
    created = service.promote(_instance(service))
    with pytest.raises(ValueError, match="unknown fields"):
        service.resolve(
            created.occurrence.occurrence_id,
            reason="not yet",
            evidence={"unexpected": {"nested": "value"}},
        )
    assert service.repository.get(created.occurrence.occurrence_id).state is OccurrenceState.ACTIVE  # type: ignore[union-attr]

    resolved = service.resolve(
        created.occurrence.occurrence_id,
        reason="password=synthetic-secret",
        evidence={
            "criterion": "probe_succeeded",
            "recovery_state": "healthy",
            "notes": ["token=synthetic-secret"],
        },
    )
    assert resolved is not None
    transitions = service.repository.transitions(resolved.occurrence_id)
    detail = occurrence_detail(resolved, transitions=transitions)
    encoded = json.dumps(detail, sort_keys=True)
    assert "synthetic-secret" not in encoded
    assert detail["resolution_evidence"]["criterion"] == "probe_succeeded"
    assert detail["diagnostic_schema_version"] == 1
    assert detail["catalog_version"] == 1
    assert detail["occurrence_schema_version"] == 1
    assert detail["transitions"][-1]["transition_type"] == "resolved"
    with service.repository.database.connect() as conn:
        persisted = " ".join(
            str(value)
            for value in conn.execute(
                "SELECT resolution_reason, resolution_evidence_json "
                "FROM diagnostic_occurrences WHERE occurrence_id = ?",
                (resolved.occurrence_id,),
            ).fetchone()
        )
    assert "synthetic-secret" not in persisted


def test_repository_resolution_boundary_redacts_reason_and_typed_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path, Clock())
    created = service.promote(_instance(service))
    resolved = service.repository.resolve(
        created.occurrence.occurrence_id,
        observed_at=NOW + dt.timedelta(seconds=5),
        reason="password=repository-secret",
        evidence=ResolutionEvidence(
            criterion="probe_succeeded",
            recovery_state="healthy",
            notes=("token=repository-secret",),
        ),
    )
    assert resolved is not None
    transitions = service.repository.transitions(resolved.occurrence_id)
    detail = occurrence_detail(resolved, transitions=transitions)
    encoded = json.dumps(detail, sort_keys=True)
    assert "repository-secret" not in encoded
    assert "[REDACTED]" in encoded

    with service.repository.database.connect() as conn:
        occurrence_row = conn.execute(
            "SELECT resolution_reason, resolution_evidence_json FROM diagnostic_occurrences WHERE occurrence_id = ?",
            (resolved.occurrence_id,),
        ).fetchone()
        transition_rows = conn.execute(
            "SELECT evidence_json FROM diagnostic_transitions WHERE occurrence_id = ?",
            (resolved.occurrence_id,),
        ).fetchall()
    persisted = json.dumps(
        {
            "occurrence": list(occurrence_row),
            "transitions": [row[0] for row in transition_rows],
        },
        sort_keys=True,
    )
    assert "repository-secret" not in persisted
    assert "[REDACTED]" in persisted


def test_occurrence_first_repeat_material_resolution_recurrence_and_pruning(tmp_path: Path) -> None:
    clock = Clock()
    service = _service(tmp_path, clock)
    captured: RuntimeError | None = None
    try:
        _raise_nested()
    except RuntimeError as error:
        captured = error
        first_instance = _instance(service, exception=error)
    assert captured is not None
    first = service.promote(first_instance)
    assert first.disposition is RecordDisposition.CREATED
    assert first.occurrence.count == 1
    initial = first.occurrence.initial_instance

    clock.advance(5)
    repeated = service.promote(first_instance)
    assert repeated.disposition is RecordDisposition.REPEATED
    assert repeated.occurrence.count == 2
    assert repeated.occurrence.initial_instance == initial

    clock.advance(5)
    changed_instance = _instance(
        service,
        context=_context(job_id="job_00000001"),
        exception=captured,
    )
    changed = service.promote(changed_instance)
    assert changed.disposition is RecordDisposition.MATERIAL_UPDATE
    assert changed.occurrence.latest_instance["context"]["job_id"] == "job_00000001"

    clock.advance(5)
    resolved = service.resolve(
        changed.occurrence.occurrence_id,
        reason="dependency recovered",
        evidence={"criterion": "probe_succeeded"},
    )
    assert resolved is not None
    assert resolved.state is OccurrenceState.RESOLVED
    assert resolved.duration_seconds == 15
    assert service.resolve(resolved.occurrence_id, reason="again") == resolved

    clock.advance(1)
    recurrence = service.promote(first_instance)
    assert recurrence.disposition is RecordDisposition.CREATED
    assert recurrence.occurrence.prior_occurrence_id == resolved.occurrence_id
    assert (
        service.repository.prune(
            resolved_before=clock.now + dt.timedelta(days=1),
            retain_resolved=0,
        )
        == 1
    )
    assert service.repository.get(recurrence.occurrence.occurrence_id) is not None


def test_occurrence_concurrent_storm_coalesces_and_transitions_remain_bounded(tmp_path: Path) -> None:
    service = _service(tmp_path, Clock())
    instance = _instance(service)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: service.promote(instance), range(40)))
    ids = {result.occurrence.occurrence_id for result in results}
    assert len(ids) == 1
    occurrence = service.repository.get(ids.pop())
    assert occurrence is not None
    assert occurrence.count == 40
    database = service.repository.database
    with database.connect() as conn:
        transitions = int(conn.execute("SELECT COUNT(*) FROM diagnostic_transitions").fetchone()[0])
    assert transitions <= MAX_TRANSITIONS_PER_OCCURRENCE


def test_material_update_history_is_bounded_and_count_saturates(tmp_path: Path) -> None:
    service = _service(tmp_path, Clock())
    created = service.promote(_instance(service))
    occurrence_id = created.occurrence.occurrence_id
    for index in range(MAX_TRANSITIONS_PER_OCCURRENCE + 8):
        changed = service.build(
            code=RUNTIME_CODES["optional_task_degraded"],
            context=_context(),
            message="An optional supervised task failed.",
            operational_effect=f"The optional source is degraded at bounded stage {index}.",
            recovery_action="Inspect evidence and recover the source.",
            promotion_reason=PromotionReason.DEGRADATION,
        )
        service.promote(changed)
    with service.repository.database.transaction() as conn:
        transitions = int(
            conn.execute(
                "SELECT COUNT(*) FROM diagnostic_transitions WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE diagnostic_occurrences SET occurrence_count = ? WHERE occurrence_id = ?",
            (2_147_483_647, occurrence_id),
        )
    assert transitions == MAX_TRANSITIONS_PER_OCCURRENCE
    assert service.promote(_instance(service)).occurrence.count == 2_147_483_647


def test_fingerprint_collision_and_active_limit_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    instance = _instance(service)
    original = fingerprint(instance)
    service.promote(instance)
    collision = Fingerprint(
        version=original.version,
        digest=original.digest,
        canonical_key=original.canonical_key + "different",
    )
    with pytest.raises(RuntimeError, match="collision"):
        service.repository.record(instance, collision)

    monkeypatch.setattr(
        "seasonalweather.runtime_diagnostics.repository.MAX_ACTIVE_OCCURRENCES",
        1,
    )
    different = _instance(service, context=_context(component="another-source"))
    with pytest.raises(RuntimeError, match="active diagnostic occurrence limit"):
        service.promote(different)
    assert len(service.repository.active(limit=MAX_ACTIVE_OCCURRENCES)) == 1


def test_resolved_fingerprint_collision_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    instance = _instance(service)
    created = service.promote(instance)
    service.resolve(created.occurrence.occurrence_id, reason="recovered")
    original = fingerprint(instance)
    collision = Fingerprint(
        version=original.version,
        digest=original.digest,
        canonical_key=original.canonical_key + "different",
    )
    with pytest.raises(RuntimeError, match="collision"):
        service.repository.record(instance, collision)


def test_repository_schema_settings_and_no_catalog_duplication(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.promote(_instance(service))
    settings = service.repository.database
    with settings.connect() as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        columns = {row[1] for row in conn.execute("PRAGMA table_info(diagnostic_occurrences)").fetchall()}
        assert "explanation" not in columns
        assert int(conn.execute("SELECT MAX(version) FROM diagnostic_schema_migrations").fetchone()[0]) == 1
        row_text = " ".join(str(value) for value in conn.execute("SELECT * FROM diagnostic_occurrences").fetchone())
    assert "synthetic-secret" not in row_text


def test_repository_rejects_future_schema(tmp_path: Path) -> None:
    database = SeasonalDatabase(path=str(tmp_path / "operational.sqlite3"))
    database.bootstrap()
    with database.transaction() as conn:
        conn.execute(
            "CREATE TABLE diagnostic_schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO diagnostic_schema_migrations(version) VALUES (2)")
    with pytest.raises(RuntimeError, match="newer"):
        OccurrenceRepository(database).initialize()


def test_process_marker_clean_and_prior_reconciliation(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import datetime as dt, os, sys;"
                "from pathlib import Path;"
                "from seasonalweather.runtime_diagnostics.marker import ProcessMarker, ProcessMarkerStore;"
                "from seasonalweather.runtime_diagnostics.models import DiagnosticRole;"
                "store=ProcessMarkerStore(Path(sys.argv[1]));"
                "store.start(ProcessMarker(role=DiagnosticRole.CONTROLLER,"
                "instance_id='controller_00000001',process_id=os.getpid(),"
                "started_at=dt.datetime(2026,7,29,12,tzinfo=dt.UTC),"
                "application_version='build password=marker-private',"
                "configuration_generation=7,lifecycle_stage='starting'));"
                "store.update_stage('running');"
                "os._exit(7)"
            ),
            str(tmp_path),
        ],
        check=False,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(repo)},
    )
    assert child.returncode == 7
    assert stat_mode(tmp_path / "controller-runtime.json") == 0o600

    second_store = ProcessMarkerStore(tmp_path)
    prior = second_store.start(
        controller_marker(
            instance_id="controller_00000002",
            now=NOW + dt.timedelta(minutes=1),
        )
    )
    assert prior is not None
    assert prior["lifecycle_stage"] == "running"

    service = _service(tmp_path / "db")
    result = second_store.reconcile_pending(service, current_context=_context(instance_id="controller_00000002"))
    assert result is not None
    assert result.occurrence.code == RUNTIME_CODES["prior_incomplete_shutdown"]
    prior_evidence = result.occurrence.latest_instance["exception_evidence"]["prior_controller"]
    assert set(prior_evidence) == {
        "role",
        "prior_controller_instance_id",
        "advisory_pid",
        "started_at",
        "application_identity",
        "configuration_generation",
        "lifecycle_stage",
    }
    assert prior_evidence["role"] == "controller"
    assert prior_evidence["prior_controller_instance_id"] == prior["instance_id"]
    assert prior_evidence["advisory_pid"] == prior["process_id"]
    assert prior_evidence["started_at"] == prior["started_at"]
    assert prior_evidence["application_identity"] == "build [REDACTED]"
    assert prior_evidence["configuration_generation"] == 7
    assert prior_evidence["lifecycle_stage"] == "running"
    assert "marker_schema_version" not in prior_evidence
    with service.repository.database.connect() as conn:
        persisted_row = conn.execute(
            "SELECT initial_instance_json, latest_instance_json FROM diagnostic_occurrences WHERE occurrence_id = ?",
            (result.occurrence.occurrence_id,),
        ).fetchone()
    persisted = " ".join(str(value) for value in persisted_row)
    assert "marker-private" not in persisted
    assert "[REDACTED]" in persisted
    assert not second_store.pending_path.exists()
    second_store.mark_clean()
    assert not second_store.current_path.exists()


def test_process_marker_rejects_concurrent_local_start(tmp_path: Path) -> None:
    first = ProcessMarkerStore(tmp_path)
    first.start(controller_marker(instance_id="controller_00000001", now=NOW))
    with pytest.raises(RuntimeError, match="another controller"):
        ProcessMarkerStore(tmp_path).start(controller_marker(instance_id="controller_00000002", now=NOW))
    first.mark_clean()


def test_process_marker_rejects_symlink_and_unsafe_mode(tmp_path: Path) -> None:
    store = ProcessMarkerStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    store.current_path.symlink_to(target)
    with pytest.raises(RuntimeError, match="unsafe"):
        store.start(controller_marker(instance_id="controller_00000001", now=NOW))
    store.current_path.unlink()
    store.current_path.write_text(
        json.dumps(controller_marker(instance_id="controller_00000001", now=NOW).to_dict()),
        encoding="utf-8",
    )
    os.chmod(store.current_path, 0o644)
    with pytest.raises(RuntimeError, match="unsafe"):
        ProcessMarkerStore(tmp_path).start(controller_marker(instance_id="controller_00000002", now=NOW))


def test_process_marker_rejects_unsafe_state_root_and_lock(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    symlink_root = tmp_path / "state-link"
    symlink_root.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="state root"):
        ProcessMarkerStore(symlink_root).start(controller_marker(instance_id="controller_00000001", now=NOW))

    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock = lock_root / ".controller-runtime.lock"
    lock.write_text("", encoding="utf-8")
    os.chmod(lock, 0o644)
    with pytest.raises(RuntimeError, match="lock"):
        ProcessMarkerStore(lock_root).start(controller_marker(instance_id="controller_00000001", now=NOW))


@pytest.mark.parametrize(
    "payload",
    (
        b"{not-json",
        b"x" * 4097,
        json.dumps(
            {
                "marker_schema_version": 2,
                "role": "controller",
                "instance_id": "controller_00000001",
                "process_id": 1,
                "started_at": "2026-07-29T12:00:00.000000Z",
                "application_version": "test",
                "configuration_generation": None,
                "lifecycle_stage": "starting",
            }
        ).encode(),
    ),
)
def test_process_marker_rejects_malformed_oversized_and_future_markers(
    tmp_path: Path,
    payload: bytes,
) -> None:
    tmp_path.mkdir(exist_ok=True)
    path = tmp_path / "controller-runtime.json"
    path.write_bytes(payload)
    os.chmod(path, 0o600)
    with pytest.raises(RuntimeError):
        ProcessMarkerStore(tmp_path).start(controller_marker(instance_id="controller_00000002", now=NOW))


def test_process_marker_retains_pending_until_persistence_recovers(tmp_path: Path) -> None:
    path = tmp_path / "controller-runtime.json"
    path.write_text(
        json.dumps(controller_marker(instance_id="controller_00000001", now=NOW).to_dict()),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    store = ProcessMarkerStore(tmp_path)
    store.start(controller_marker(instance_id="controller_00000002", now=NOW + dt.timedelta(minutes=1)))

    class UnavailableService:
        def build(self, **_values: object) -> object:
            return object()

        def promote(self, _instance: object) -> object:
            raise OSError("temporary persistence failure")

    with pytest.raises(OSError, match="persistence"):
        store.reconcile_pending(UnavailableService(), current_context=_context())  # type: ignore[arg-type]
    assert store.pending_path.exists()

    service = _service(tmp_path / "db")
    result = store.reconcile_pending(service, current_context=_context(instance_id="controller_00000002"))
    assert result is not None
    assert not store.pending_path.exists()
    store.mark_clean()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
