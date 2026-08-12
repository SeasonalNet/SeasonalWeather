"""Serializable, bounded, redaction-enforcing environmental preflight."""

from __future__ import annotations

import asyncio
import math
import multiprocessing
import os
import shutil
import signal
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Protocol, TypeAlias

from .limits import VALIDATION_ENVELOPE_SECONDS

_MAX_PROBES = 64
_MAX_CONCURRENT_PROBES = 4
_WORKER_STOP_SECONDS = 0.25


class ProbeStatus(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"
    INDETERMINATE = "indeterminate"


class ProbeFailureKind(StrEnum):
    TIMEOUT = "timeout"
    INTERNAL_FAILURE = "internal_failure"


class ProbeRedaction(StrEnum):
    IDENTIFIER_ONLY = "identifier_only"
    LOCAL_PATH_BASENAME = "local_path_basename"
    ENDPOINT_HOST_OMITTED = "endpoint_host_omitted"


@dataclass(frozen=True)
class ProbeObservation:
    status: ProbeStatus
    summary: str
    retryable: bool = False
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.summary or len(self.summary) > 4_096:
            raise ValueError("probe summary is empty or overlong")
        if len(self.evidence) > 16 or any(not item or len(item) > 4_096 for item in self.evidence):
            raise ValueError("probe evidence is empty, overlong, or unbounded")


@dataclass(frozen=True)
class LocalPathSpecification:
    path: str
    directory: bool
    regular_file: bool = False

    def __post_init__(self) -> None:
        _bounded_local_value(self.path, "local path")
        if type(self.directory) is not bool:
            raise TypeError("local path directory flag must be boolean")
        if type(self.regular_file) is not bool:
            raise TypeError("local path regular-file flag must be boolean")


@dataclass(frozen=True)
class LocalExecutableSpecification:
    command: str

    def __post_init__(self) -> None:
        _bounded_local_value(self.command, "executable command")


@dataclass(frozen=True)
class LocalFileSeparationSpecification:
    first_path: str
    second_path: str

    def __post_init__(self) -> None:
        _bounded_local_value(self.first_path, "first local path")
        _bounded_local_value(self.second_path, "second local path")


ProbeSpecification: TypeAlias = LocalPathSpecification | LocalExecutableSpecification | LocalFileSeparationSpecification


def _bounded_local_value(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 4_096 or "\x00" in value:
        raise ValueError(f"probe {field_name} is empty, overlong, or unsafe")


@dataclass(frozen=True)
class PreflightProbe:
    identifier: str
    owner: str
    timeout_seconds: float
    required: bool
    fallback_available: bool
    redaction: ProbeRedaction
    specification: ProbeSpecification
    cancellation_safe: bool = True
    display_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "display_evidence", tuple(self.display_evidence))
        _validate_probe_contract(self)


def _validate_probe_contract(probe: PreflightProbe) -> None:
    _bounded_probe_name(probe.identifier, "identifier")
    _bounded_probe_name(probe.owner, "owner")
    _validate_probe_policy(probe)
    _validate_probe_specification(probe.specification)
    _validate_display_evidence(probe.display_evidence)


def _validate_probe_policy(probe: PreflightProbe) -> None:
    if not 0.01 <= probe.timeout_seconds <= 30.0:
        raise ValueError("probe timeout is outside the bounded range")
    if type(probe.required) is not bool or type(probe.fallback_available) is not bool:
        raise TypeError("probe requirement policy must be boolean")
    if not probe.cancellation_safe:
        raise ValueError("preflight probes must declare cancellation safety")


def _validate_probe_specification(specification: ProbeSpecification) -> None:
    if not isinstance(
        specification,
        LocalPathSpecification | LocalExecutableSpecification | LocalFileSeparationSpecification,
    ):
        raise TypeError("preflight probe specification is not framework-owned")


def _validate_display_evidence(evidence: tuple[str, ...]) -> None:
    if len(evidence) > 8 or any(not isinstance(item, str) or not item or len(item) > 128 for item in evidence):
        raise ValueError("framework-owned probe display evidence is unbounded")


def _bounded_probe_name(value: str, field_name: str) -> None:
    if not value or len(value) > 64:
        raise ValueError(f"probe {field_name} is empty or overlong")


@dataclass(frozen=True)
class PreflightResult:
    identifier: str
    owner: str
    status: ProbeStatus
    required: bool
    fallback_available: bool
    blocking: bool
    redaction: ProbeRedaction
    summary: str
    retryable: bool
    elapsed_milliseconds: int
    failure_kind: ProbeFailureKind | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if len(self.summary) > 256 or len(self.evidence) > 8:
            raise ValueError("preflight result evidence is unbounded")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "identifier": self.identifier,
            "owner": self.owner,
            "status": self.status.value,
            "required": self.required,
            "fallback_available": self.fallback_available,
            "blocking": self.blocking,
            "redaction": self.redaction.value,
            "summary": self.summary,
            "retryable": self.retryable,
            "elapsed_milliseconds": self.elapsed_milliseconds,
            "evidence": list(self.evidence),
        }
        if self.failure_kind is not None:
            result["failure_kind"] = self.failure_kind.value
        return result


class ProbeExecutor(Protocol):
    async def observe(
        self,
        probe: PreflightProbe,
        monotonic: Callable[[], float],
    ) -> tuple[ProbeObservation, ProbeFailureKind | None]: ...


class _ProcessHandle(Protocol):
    @property
    def pid(self) -> int | None: ...

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...


class _SpawnProbeExecutor:
    """Dedicated safe-start helper; worker-target injection is test-only."""

    def __init__(self, worker_target: Callable[[ProbeSpecification, Connection], None] | None = None) -> None:
        self._worker_target = worker_target or _framework_worker

    async def observe(
        self,
        probe: PreflightProbe,
        monotonic: Callable[[], float],
    ) -> tuple[ProbeObservation, ProbeFailureKind | None]:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        worker = context.Process(
            target=_process_group_worker,
            args=(self._worker_target, probe.specification, sender),
            name=f"preflight-{probe.identifier}",
            daemon=False,
        )
        started = False
        try:
            worker.start()
            started = True
            sender.close()
            return await _poll_worker(
                receiver,
                worker,
                deadline=monotonic() + probe.timeout_seconds,
                monotonic=monotonic,
            )
        except (OSError, RuntimeError, TypeError):
            return _failed_observation(ProbeFailureKind.INTERNAL_FAILURE)
        finally:
            receiver.close()
            sender.close()
            if started:
                _stop_worker_group(worker)


def _process_group_worker(
    target: Callable[[ProbeSpecification, Connection], None],
    specification: ProbeSpecification,
    output: Connection,
) -> None:
    try:
        os.setsid()
        signal.signal(signal.SIGTERM, _terminate_worker_group)
        output.send(("ready", None))
        target(specification, output)
    except BaseException as exc:
        with suppress(BrokenPipeError, EOFError, OSError):
            output.send(("error", type(exc).__name__))
    finally:
        output.close()


def _terminate_worker_group(_signum: int, _frame: object) -> None:
    """Let descendants receive SIGTERM, reap direct children, then exit."""

    deadline = time.monotonic() + _WORKER_STOP_SECONDS
    while time.monotonic() < deadline:
        try:
            child, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if child == 0:
            time.sleep(0.005)
    os._exit(143)


def _framework_worker(specification: ProbeSpecification, output: Connection) -> None:
    observation = _validated_worker_observation(_dispatch_framework_probe(specification))
    output.send(("result", observation))


def _dispatch_framework_probe(specification: ProbeSpecification) -> ProbeObservation:
    if isinstance(specification, LocalPathSpecification):
        selected = Path(specification.path)
        exists = selected.is_dir() if specification.directory else selected.is_file()
        if specification.regular_file:
            exists = exists and not selected.is_symlink()
        return ProbeObservation(
            ProbeStatus.AVAILABLE if exists else ProbeStatus.UNAVAILABLE,
            "Local path check completed.",
            retryable=not exists,
        )
    if isinstance(specification, LocalExecutableSpecification):
        exists = shutil.which(specification.command) is not None
        return ProbeObservation(
            ProbeStatus.AVAILABLE if exists else ProbeStatus.UNAVAILABLE,
            "Executable lookup completed.",
        )
    if isinstance(specification, LocalFileSeparationSpecification):
        return _file_separation_observation(specification)
    raise TypeError("probe specification is not admitted")


def _file_separation_observation(specification: LocalFileSeparationSpecification) -> ProbeObservation:
    try:
        first = Path(specification.first_path).resolve(strict=False)
        second = Path(specification.second_path).resolve(strict=False)
        if first == second:
            same_file = True
        else:
            first_exists = _target_exists(first)
            second_exists = _target_exists(second)
            same_file = os.path.samefile(first, second) if first_exists and second_exists else False
    except (OSError, RuntimeError):
        return ProbeObservation(
            ProbeStatus.INDETERMINATE,
            "Physical path identity could not be determined.",
            retryable=True,
        )
    return ProbeObservation(
        ProbeStatus.UNAVAILABLE if same_file else ProbeStatus.AVAILABLE,
        "Physical path identity check completed.",
    )


def _target_exists(path: Path) -> bool:
    try:
        path.stat()
    except FileNotFoundError:
        return False
    return True


def _validated_worker_observation(value: object) -> ProbeObservation:
    if type(value) is not ProbeObservation:
        raise TypeError("probe returned an unsupported observation")
    if (
        not isinstance(value.status, ProbeStatus)
        or type(value.summary) is not str
        or type(value.retryable) is not bool
        or type(value.evidence) is not tuple
    ):
        raise TypeError("probe observation fields have unsupported types")
    return ProbeObservation(value.status, value.summary, retryable=value.retryable, evidence=value.evidence)


def _stop_worker_group(worker: _ProcessHandle) -> None:
    """Terminate the complete worker session and reap its process."""

    pid = worker.pid
    if pid is None:
        return
    _signal_process_group(pid, signal.SIGTERM)
    worker.join(_WORKER_STOP_SECONDS)
    _signal_process_group(pid, signal.SIGKILL)
    if worker.is_alive():
        worker.kill()
    worker.join(_WORKER_STOP_SECONDS)
    if worker.is_alive():
        raise RuntimeError("preflight worker group could not be terminated")
    worker.join(0)


def _signal_process_group(group_id: int, selected_signal: signal.Signals) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(group_id, selected_signal)


async def _poll_worker(
    receiver: Connection,
    worker: _ProcessHandle,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> tuple[ProbeObservation, ProbeFailureKind | None]:
    ready = False
    while True:
        if receiver.poll():
            try:
                kind, value = receiver.recv()
            except EOFError:
                return _failed_observation(ProbeFailureKind.INTERNAL_FAILURE)
            if kind == "ready":
                ready = True
            elif kind == "result" and isinstance(value, ProbeObservation):
                return value, None
            else:
                return _failed_observation(ProbeFailureKind.INTERNAL_FAILURE)
        remaining = deadline - monotonic()
        if remaining <= 0:
            return _failed_observation(ProbeFailureKind.TIMEOUT)
        if not worker.is_alive():
            return _failed_observation(ProbeFailureKind.INTERNAL_FAILURE)
        await asyncio.sleep(min(0.005 if ready else 0.001, remaining))


def _failed_observation(
    kind: ProbeFailureKind,
) -> tuple[ProbeObservation, ProbeFailureKind]:
    summary = "Probe timed out." if kind is ProbeFailureKind.TIMEOUT else "Probe failed internally."
    return ProbeObservation(ProbeStatus.INDETERMINATE, summary, retryable=True), kind


def _redacted_observation(probe: PreflightProbe, observation: ProbeObservation) -> ProbeObservation:
    evidence: tuple[str, ...] = ()
    if probe.redaction is ProbeRedaction.LOCAL_PATH_BASENAME:
        evidence = tuple(item for item in probe.display_evidence if safe_probe_evidence(item))
    return ProbeObservation(
        observation.status,
        canonical_probe_summary(observation.status),
        retryable=observation.retryable,
        evidence=evidence,
    )


def canonical_probe_summary(status: ProbeStatus) -> str:
    return {
        ProbeStatus.AVAILABLE: "Probe reports the dependency is available.",
        ProbeStatus.DEGRADED: "Probe reports the dependency is degraded.",
        ProbeStatus.UNAVAILABLE: "Probe reports the dependency is unavailable.",
        ProbeStatus.SKIPPED: "Probe was skipped.",
        ProbeStatus.UNSUPPORTED: "Probe reports the dependency is unsupported.",
        ProbeStatus.INDETERMINATE: "Probe result is indeterminate.",
    }[status]


def safe_probe_basename(value: str) -> bool:
    if len(value) > 128 or value in {"", ".", ".."}:
        return False
    if any(marker in value for marker in ("/", "\\", "?", "#", ":", "@", "=")):
        return False
    return all(character.isalnum() or character in "._-" for character in value)


def safe_probe_evidence(value: str) -> bool:
    if not safe_probe_basename(value):
        return False
    folded = value.casefold()
    secret_markers = (
        "authorization",
        "bearer",
        "credential",
        "endpoint",
        "password",
        "private",
        "secret",
        "sentinel",
        "token",
        "webhook",
    )
    return not folded.startswith("sk-") and not any(marker in folded for marker in secret_markers)


async def _observe(
    probe: PreflightProbe,
    monotonic: Callable[[], float],
    executor: ProbeExecutor,
    semaphore: asyncio.Semaphore,
    envelope_deadline: float,
) -> PreflightResult:
    async with semaphore:
        started = monotonic()
        remaining = envelope_deadline - started
        observation: ProbeObservation
        failure_kind: ProbeFailureKind | None
        if remaining <= 0:
            observation, failure_kind = _failed_observation(ProbeFailureKind.TIMEOUT)
        else:
            try:
                observation, failure_kind = await asyncio.wait_for(
                    executor.observe(probe, monotonic),
                    timeout=min(probe.timeout_seconds, remaining),
                )
            except TimeoutError:
                observation, failure_kind = _failed_observation(ProbeFailureKind.TIMEOUT)
            except Exception:
                observation, failure_kind = _failed_observation(ProbeFailureKind.INTERNAL_FAILURE)
        observation = _redacted_observation(probe, observation)
        elapsed = max(0, min(30_000, int((monotonic() - started) * 1000)))
    blocking = (
        probe.required
        and not probe.fallback_available
        and observation.status in {ProbeStatus.UNAVAILABLE, ProbeStatus.UNSUPPORTED, ProbeStatus.INDETERMINATE}
    )
    return PreflightResult(
        identifier=probe.identifier,
        owner=probe.owner,
        status=observation.status,
        required=probe.required,
        fallback_available=probe.fallback_available,
        blocking=blocking,
        redaction=probe.redaction,
        summary=observation.summary,
        retryable=observation.retryable,
        elapsed_milliseconds=elapsed,
        failure_kind=failure_kind,
        evidence=observation.evidence,
    )


async def run_preflight(
    probes: tuple[PreflightProbe, ...],
    *,
    monotonic: Callable[[], float] = time.monotonic,
    executor: ProbeExecutor | None = None,
    deadline: float | None = None,
) -> tuple[PreflightResult, ...]:
    """Run probes within the shared validation envelope and worker ceiling."""

    selected = tuple(probes)
    if len(selected) > _MAX_PROBES:
        raise ValueError("preflight probe count exceeds the bound")
    identifiers = tuple(probe.identifier for probe in selected)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("preflight probe identifiers must be unique")
    envelope_deadline = _bounded_envelope_deadline(monotonic(), deadline)
    selected_executor = executor or _SpawnProbeExecutor()
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PROBES)
    tasks = tuple(
        asyncio.create_task(
            _observe(
                probe,
                monotonic,
                selected_executor,
                semaphore,
                envelope_deadline,
            )
        )
        for probe in selected
    )
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if monotonic() > envelope_deadline:
        raise TimeoutError("preflight exceeded the validation envelope")
    return tuple(sorted(results, key=lambda item: item.identifier))


def _bounded_envelope_deadline(started: float, requested: float | None) -> float:
    maximum = started + VALIDATION_ENVELOPE_SECONDS
    if requested is None:
        return maximum
    if isinstance(requested, bool) or not isinstance(requested, float | int) or not math.isfinite(requested):
        raise ValueError("preflight deadline is malformed")
    return min(maximum, requested)


def local_path_probe(
    *,
    identifier: str,
    owner: str,
    path: str,
    required: bool,
    directory: bool,
    regular_file: bool = False,
    fallback_available: bool = False,
    timeout_seconds: float = 1.0,
) -> PreflightProbe:
    selected = Path(path)
    return PreflightProbe(
        identifier=identifier,
        owner=owner,
        timeout_seconds=timeout_seconds,
        required=required,
        fallback_available=fallback_available,
        redaction=ProbeRedaction.LOCAL_PATH_BASENAME,
        specification=LocalPathSpecification(path, directory, regular_file),
        display_evidence=(selected.name,),
    )


def local_executable_probe(
    *,
    identifier: str,
    owner: str,
    command: str,
    required: bool,
    fallback_available: bool = False,
    timeout_seconds: float = 1.0,
) -> PreflightProbe:
    return PreflightProbe(
        identifier=identifier,
        owner=owner,
        timeout_seconds=timeout_seconds,
        required=required,
        fallback_available=fallback_available,
        redaction=ProbeRedaction.IDENTIFIER_ONLY,
        specification=LocalExecutableSpecification(command),
    )


def local_file_separation_probe(
    *,
    identifier: str,
    owner: str,
    first_path: str,
    second_path: str,
    required: bool = True,
    timeout_seconds: float = 1.0,
) -> PreflightProbe:
    return PreflightProbe(
        identifier=identifier,
        owner=owner,
        timeout_seconds=timeout_seconds,
        required=required,
        fallback_available=False,
        redaction=ProbeRedaction.IDENTIFIER_ONLY,
        specification=LocalFileSeparationSpecification(first_path, second_path),
    )
