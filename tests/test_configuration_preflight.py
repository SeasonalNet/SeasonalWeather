from __future__ import annotations

import asyncio
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from seasonalweather.configuration import compile_path
from seasonalweather.validation import (
    VALIDATION_ENVELOPE_SECONDS,
    PreflightProbe,
    ProbeFailureKind,
    ProbeObservation,
    ProbeRedaction,
    ProbeStatus,
    configured_preflight_probes,
    local_executable_probe,
    local_file_separation_probe,
    local_path_probe,
    run_preflight,
)
from seasonalweather.validation.preflight import (
    LocalPathSpecification,
    _poll_worker,
    _SpawnProbeExecutor,
)


def _probe(
    identifier: str,
    *,
    required: bool,
    fallback: bool = False,
    timeout: float = 0.05,
    redaction: ProbeRedaction = ProbeRedaction.IDENTIFIER_ONLY,
    display_evidence: tuple[str, ...] = (),
) -> PreflightProbe:
    return PreflightProbe(
        identifier=identifier,
        owner="test",
        timeout_seconds=timeout,
        required=required,
        fallback_available=fallback,
        redaction=redaction,
        specification=LocalPathSpecification("/unused-test-fixture", directory=False),
        display_evidence=display_evidence,
    )


class _ScriptedExecutor:
    def __init__(
        self,
        observations: dict[str, ProbeObservation | BaseException] | None = None,
        *,
        blocked: frozenset[str] = frozenset(),
        delay: float = 0,
    ) -> None:
        self.observations = observations or {}
        self.blocked = blocked
        self.delay = delay
        self.active = 0
        self.maximum_active = 0

    async def observe(self, probe, monotonic):
        del monotonic
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            if probe.identifier in self.blocked:
                await asyncio.Event().wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            selected = self.observations.get(
                probe.identifier,
                ProbeObservation(ProbeStatus.AVAILABLE, "available"),
            )
            if isinstance(selected, BaseException):
                raise selected
            return selected, None
        finally:
            self.active -= 1


def _defective_tree_worker(specification, output) -> None:
    del output
    assert isinstance(specification, LocalPathSpecification)
    child = subprocess.Popen(  # noqa: S603 - injected defective test worker
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=False,
    )
    Path(specification.path).write_text(f"{os.getpid()} {child.pid}", encoding="ascii")
    while True:
        time.sleep(60)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _read_pid_pair(path: Path) -> tuple[int, int] | None:
    try:
        values = path.read_text(encoding="ascii").split()
        if len(values) != 2:
            return None
        return int(values[0]), int(values[1])
    except (OSError, ValueError):
        return None


class _QueuedWorkerPipe:
    def __init__(self, messages: list[object], *, eof: bool = False) -> None:
        self.messages = messages
        self.eof = eof

    def poll(self) -> bool:
        return bool(self.messages) or self.eof

    def recv(self) -> object:
        if self.messages:
            return self.messages.pop(0)
        raise EOFError


class _ScriptedWorker:
    pid = 12345

    def __init__(self, alive: bool) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


def _poll_protocol(
    messages: list[object], *, eof: bool = False, alive: bool = False
) -> tuple[ProbeObservation, ProbeFailureKind | None]:
    receiver = _QueuedWorkerPipe(messages, eof=eof)
    worker = _ScriptedWorker(alive)
    return asyncio.run(
        _poll_worker(
            receiver,  # type: ignore[arg-type]
            worker,
            deadline=1.0,
            monotonic=lambda: 0.0,
        )
    )


def test_poll_worker_dead_after_ready_prefers_queued_result() -> None:
    observation, failure_kind = _poll_protocol(
        [("ready", None), ("result", ProbeObservation(ProbeStatus.AVAILABLE, "available"))]
    )

    assert observation.status is ProbeStatus.AVAILABLE
    assert failure_kind is None


def test_poll_worker_dead_after_ready_without_result_is_internal_failure() -> None:
    observation, failure_kind = _poll_protocol([("ready", None)], eof=True)

    assert observation.status is ProbeStatus.INDETERMINATE
    assert failure_kind is ProbeFailureKind.INTERNAL_FAILURE


def test_poll_worker_error_terminal_message_is_internal_failure() -> None:
    observation, failure_kind = _poll_protocol([("ready", None), ("error", "RuntimeError")])

    assert observation.status is ProbeStatus.INDETERMINATE
    assert failure_kind is ProbeFailureKind.INTERNAL_FAILURE


def test_poll_worker_deadline_expiry_is_timeout() -> None:
    receiver = _QueuedWorkerPipe([])
    worker = _ScriptedWorker(alive=True)
    observation, failure_kind = asyncio.run(
        _poll_worker(receiver, worker, deadline=1.0, monotonic=lambda: 1.0)  # type: ignore[arg-type]
    )

    assert observation.status is ProbeStatus.INDETERMINATE
    assert failure_kind is ProbeFailureKind.TIMEOUT


def test_required_optional_degraded_and_exception_results_are_policy_distinct() -> None:
    executor = _ScriptedExecutor(
        {
            "healthy": ProbeObservation(ProbeStatus.AVAILABLE, "available"),
            "required": ProbeObservation(ProbeStatus.UNAVAILABLE, "unavailable", retryable=True),
            "optional": ProbeObservation(ProbeStatus.UNAVAILABLE, "unavailable", retryable=True),
            "degraded": ProbeObservation(ProbeStatus.DEGRADED, "usable through fallback"),
            "failed": RuntimeError("SENTINEL-PRIVATE-ENDPOINT?token=secret"),
        }
    )
    probes = (
        _probe("healthy", required=True),
        _probe("required", required=True),
        _probe("optional", required=False),
        _probe("degraded", required=True, fallback=True),
        _probe("failed", required=False),
    )

    results = asyncio.run(run_preflight(probes, executor=executor))
    by_id = {item.identifier: item for item in results}

    assert by_id["healthy"].status is ProbeStatus.AVAILABLE
    assert by_id["required"].blocking
    assert not by_id["optional"].blocking
    assert not by_id["degraded"].blocking
    assert by_id["failed"].status is ProbeStatus.INDETERMINATE
    assert by_id["failed"].failure_kind is ProbeFailureKind.INTERNAL_FAILURE
    assert "SENTINEL" not in by_id["failed"].summary


def test_hung_optional_probe_times_out_without_suppressing_other_results() -> None:
    executor = _ScriptedExecutor(blocked=frozenset({"hung"}))
    results = asyncio.run(
        run_preflight(
            (_probe("hung", required=False, timeout=0.02), _probe("other", required=True)),
            executor=executor,
        )
    )
    by_id = {item.identifier: item for item in results}

    assert by_id["hung"].status is ProbeStatus.INDETERMINATE
    assert by_id["hung"].failure_kind is ProbeFailureKind.TIMEOUT
    assert not by_id["hung"].blocking
    assert by_id["other"].status is ProbeStatus.AVAILABLE


def test_preflight_cancellation_propagates_to_injected_executor() -> None:
    async def scenario() -> None:
        executor = _ScriptedExecutor(blocked=frozenset({"blocked"}))
        task = asyncio.create_task(run_preflight((_probe("blocked", required=False, timeout=1.0),), executor=executor))
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert executor.active == 0

    asyncio.run(scenario())


def test_overall_validation_envelope_cancels_preflight_and_cannot_be_widened() -> None:
    async def scenario() -> None:
        executor = _ScriptedExecutor(blocked=frozenset({"blocked"}))
        now = time.monotonic()
        with pytest.raises(TimeoutError, match="validation envelope"):
            await run_preflight(
                (_probe("blocked", required=False, timeout=1.0),),
                executor=executor,
                deadline=now + 0.02,
            )
        assert executor.active == 0

        readings = iter((0.0, 601.0, 601.0, 601.0))
        widened_executor = _ScriptedExecutor()
        with pytest.raises(TimeoutError, match="validation envelope"):
            await run_preflight(
                (_probe("widened", required=False, timeout=1.0),),
                monotonic=lambda: next(readings),
                executor=widened_executor,
                deadline=10_000.0,
            )
        assert widened_executor.maximum_active == 0

    assert VALIDATION_ENVELOPE_SECONDS == 600
    asyncio.run(scenario())


def test_probe_contract_rejects_arbitrary_callback_specification() -> None:
    with pytest.raises(TypeError, match="framework-owned"):
        PreflightProbe(
            identifier="callback",
            owner="test",
            timeout_seconds=0.1,
            required=False,
            fallback_available=False,
            redaction=ProbeRedaction.IDENTIFIER_ONLY,
            specification=lambda: ProbeObservation(ProbeStatus.AVAILABLE, "unsafe"),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "redaction",
    (
        ProbeRedaction.IDENTIFIER_ONLY,
        ProbeRedaction.LOCAL_PATH_BASENAME,
        ProbeRedaction.ENDPOINT_HOST_OMITTED,
    ),
)
def test_probe_redaction_is_enforced_on_all_returned_summary_and_evidence(redaction: ProbeRedaction) -> None:
    executor = _ScriptedExecutor(
        {
            "redaction": ProbeObservation(
                ProbeStatus.DEGRADED,
                "SENTINEL /private/secret https://host/path?token=secret",
                evidence=("safe-name.wav", "/private/SENTINEL", "token=secret"),
            )
        }
    )
    result = asyncio.run(
        run_preflight(
            (
                _probe(
                    "redaction",
                    required=False,
                    fallback=True,
                    redaction=redaction,
                    display_evidence=("safe-name.wav",),
                ),
            ),
            executor=executor,
        )
    )[0]
    rendered = str(result.to_dict())

    assert "SENTINEL" not in rendered
    assert "secret" not in rendered
    assert "https://" not in rendered
    assert result.evidence == (("safe-name.wav",) if redaction is ProbeRedaction.LOCAL_PATH_BASENAME else ())


def test_small_concurrency_ceiling_bounds_sixty_four_probes() -> None:
    executor = _ScriptedExecutor(delay=0.01)
    probes = tuple(_probe(f"probe-{index:02d}", required=False, timeout=1.0) for index in range(64))

    results = asyncio.run(run_preflight(probes, executor=executor))

    assert len(results) == 64
    assert executor.maximum_active == 4


def test_timeout_terminates_and_reaps_worker_and_descendant_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "process-tree.pids"
    probe = PreflightProbe(
        identifier="defective-tree",
        owner="test",
        timeout_seconds=0.5,
        required=False,
        fallback_available=False,
        redaction=ProbeRedaction.IDENTIFIER_ONLY,
        specification=LocalPathSpecification(str(pid_file), directory=False),
    )
    baseline = {child.pid for child in multiprocessing.active_children()}

    result = asyncio.run(run_preflight((probe,), executor=_SpawnProbeExecutor(worker_target=_defective_tree_worker)))[0]
    pid_pair = _read_pid_pair(pid_file)

    assert result.failure_kind is ProbeFailureKind.TIMEOUT
    assert pid_pair is not None
    worker_pid, child_pid = pid_pair
    assert not _pid_exists(worker_pid)
    assert not _pid_exists(child_pid)
    assert {child.pid for child in multiprocessing.active_children()} == baseline


def test_cancellation_terminates_and_reaps_worker_and_descendant_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "cancelled-tree.pids"
    probe = PreflightProbe(
        identifier="cancelled-tree",
        owner="test",
        timeout_seconds=5.0,
        required=False,
        fallback_available=False,
        redaction=ProbeRedaction.IDENTIFIER_ONLY,
        specification=LocalPathSpecification(str(pid_file), directory=False),
    )

    async def scenario() -> tuple[int, int]:
        task = asyncio.create_task(
            run_preflight((probe,), executor=_SpawnProbeExecutor(worker_target=_defective_tree_worker))
        )
        pid_pair = None
        for _ in range(200):
            pid_pair = _read_pid_pair(pid_file)
            if pid_pair is not None:
                break
            await asyncio.sleep(0.005)
        assert pid_pair is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid_pair

    worker_pid, child_pid = asyncio.run(scenario())

    assert not _pid_exists(worker_pid)
    assert not _pid_exists(child_pid)


def test_physical_file_separation_detects_lexical_symlink_and_hardlink_aliases(tmp_path: Path) -> None:
    first = tmp_path / "jobs.sqlite3"
    distinct = tmp_path / "operational.sqlite3"
    symlink = tmp_path / "alias.sqlite3"
    hardlink = tmp_path / "hardlink.sqlite3"
    first.write_text("jobs", encoding="utf-8")
    distinct.write_text("operational", encoding="utf-8")
    symlink.symlink_to(first)
    os.link(first, hardlink)

    probes = (
        local_file_separation_probe(identifier="same", owner="test", first_path=str(first), second_path=str(first)),
        local_file_separation_probe(
            identifier="distinct", owner="test", first_path=str(first), second_path=str(distinct)
        ),
        local_file_separation_probe(
            identifier="symlink", owner="test", first_path=str(first), second_path=str(symlink)
        ),
        local_file_separation_probe(
            identifier="hardlink", owner="test", first_path=str(first), second_path=str(hardlink)
        ),
    )
    by_id = {item.identifier: item for item in asyncio.run(run_preflight(probes))}

    assert by_id["same"].status is ProbeStatus.UNAVAILABLE
    assert by_id["distinct"].status is ProbeStatus.AVAILABLE
    assert by_id["symlink"].status is ProbeStatus.UNAVAILABLE
    assert by_id["hardlink"].status is ProbeStatus.UNAVAILABLE
    assert all(not item.evidence for item in by_id.values())


def test_distinct_nonexistent_targets_are_physically_distinct_and_redacted(tmp_path: Path) -> None:
    first = tmp_path / "SENTINEL-PRIVATE-ENDPOINT?token=secret"
    second = tmp_path / "sk-supersecret123"
    result = asyncio.run(
        run_preflight(
            (
                local_file_separation_probe(
                    identifier="nonexistent",
                    owner="test",
                    first_path=str(first),
                    second_path=str(second),
                ),
            )
        )
    )[0]

    assert result.status is ProbeStatus.AVAILABLE
    assert not result.blocking
    rendered = str(result.to_dict()).casefold()
    assert "sentinel" not in rendered
    assert "secret" not in rendered
    assert "token=" not in rendered


def test_same_nonexistent_resolved_target_is_not_distinct(tmp_path: Path) -> None:
    first = tmp_path / "missing" / ".." / "same.sqlite3"
    second = tmp_path / "same.sqlite3"
    result = asyncio.run(
        run_preflight(
            (
                local_file_separation_probe(
                    identifier="same-missing",
                    owner="test",
                    first_path=str(first),
                    second_path=str(second),
                ),
            )
        )
    )[0]

    assert result.status is ProbeStatus.UNAVAILABLE
    assert result.blocking


def test_file_separation_reserves_indeterminate_for_genuine_io_uncertainty(tmp_path: Path) -> None:
    first = tmp_path / "loop-a"
    second = tmp_path / "loop-b"
    first.symlink_to(second)
    second.symlink_to(first)
    result = asyncio.run(
        run_preflight(
            (
                local_file_separation_probe(
                    identifier="symlink-loop",
                    owner="test",
                    first_path=str(first),
                    second_path=str(tmp_path / "other"),
                ),
            )
        )
    )[0]

    assert result.status is ProbeStatus.INDETERMINATE
    assert result.blocking


def test_configuration_probe_factory_is_validation_owned_and_cli_independent() -> None:
    compiled = compile_path(Path(__file__).resolve().parents[1] / "config/config.yaml", environ={})

    probes = configured_preflight_probes(compiled)

    assert "path.work_dir" in {probe.identifier for probe in probes}
    cli_source = (Path(__file__).resolve().parents[1] / "seasonalweather/cli/config.py").read_text(encoding="utf-8")
    assert "local_file_separation_probe" not in cli_source
    assert "configured_preflight_probes(compiled)" in cli_source


@pytest.mark.parametrize(
    "basename",
    (
        "sk-supersecret123",
        "Bearer-private-value",
        "https:__host_query_token",
        "absolute_private_path",
        "SENTINEL-PRIVATE-ENDPOINT",
    ),
)
def test_framework_path_evidence_applies_final_secret_redaction(tmp_path: Path, basename: str) -> None:
    selected = tmp_path / basename
    selected.write_text("fixture", encoding="utf-8")
    result = asyncio.run(
        run_preflight(
            (local_path_probe(identifier="path", owner="test", path=str(selected), required=True, directory=False),)
        )
    )[0]

    assert result.status is ProbeStatus.AVAILABLE
    assert not result.evidence
    rendered = str(result.to_dict()).casefold()
    assert "supersecret" not in rendered
    assert "bearer" not in rendered
    assert "token" not in rendered
    assert "sentinel" not in rendered


def test_production_executable_probe_is_framework_owned_and_read_only() -> None:
    result = asyncio.run(
        run_preflight(
            (
                local_executable_probe(
                    identifier="python",
                    owner="test",
                    command=str(Path(sys.executable).resolve()),
                    required=True,
                ),
            )
        )
    )[0]

    assert result.status is ProbeStatus.AVAILABLE
    assert not result.evidence


def test_remote_credential_probe_requires_a_regular_non_symlink_file(tmp_path: Path) -> None:
    target = tmp_path / "credential"
    target.write_text("fixture", encoding="ascii")
    link = tmp_path / "credential-link"
    link.symlink_to(target)
    fifo = tmp_path / "credential-fifo"
    os.mkfifo(fifo)
    results = asyncio.run(
        run_preflight(
            (
                local_path_probe(
                    identifier="credential-link",
                    owner="tts",
                    path=str(link),
                    required=True,
                    directory=False,
                    regular_file=True,
                ),
                local_path_probe(
                    identifier="credential-fifo",
                    owner="tts",
                    path=str(fifo),
                    required=True,
                    directory=False,
                    regular_file=True,
                ),
            )
        )
    )
    assert all(result.status is ProbeStatus.UNAVAILABLE for result in results)
