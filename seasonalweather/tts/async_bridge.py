"""One bounded async-to-sync boundary for production TTS callers."""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import os
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import CancelledError as ConcurrentCancelledError
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

from .cancellation import StopCause, SynthesisStop
from .models import (
    BackendId,
    FinalizationContext,
    SynthesisDisposition,
    SynthesisFailure,
    SynthesisPurpose,
    SynthesisRequest,
    SynthesisResult,
)
from .policy import deadline_for
from .subprocess import ProcessFailure
from .tts import TTS

DEFAULT_SHUTDOWN_SECONDS = 5.0


@dataclass(frozen=True)
class FinalizationEvidence:
    """Typed evidence for the private artifact completed by a finalizer."""

    staged_path: Path


@dataclass(frozen=True)
class _WorkerCompletion:
    result: object
    completed_at: float | None = None
    publication_decision_at: float | None = None


Finalize = Callable[[Path, object, Callable[[], None]], FinalizationEvidence]
_ExecutionResult = TypeVar("_ExecutionResult")


class EmbeddedExecutionPort(Executor):
    """Controller-composed embedded P1-06 execution port.

    The executor is shared by all synthesis calls and created lazily.  It is
    deliberately not a scheduler: P1-06 priority metadata remains attached to
    the request, while Phase 1 direct execution provides bounded submission
    and one controller-owned execution lane only.
    """

    def __init__(self, *, max_workers: int = 1) -> None:
        if max_workers != 1:
            raise ValueError("embedded TTS execution is intentionally one lane in Phase 1")
        self._executor: ThreadPoolExecutor | None = None

    def submit(self, fn: Callable[..., _ExecutionResult], *args: Any, **kwargs: Any) -> Future[_ExecutionResult]:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="seasonalweather-tts")
        return self._executor.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = True) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            self._executor = None


def _stop_result(request: object, cause: StopCause, started: float) -> object:
    disposition = SynthesisDisposition.TIMED_OUT if cause is StopCause.TIMED_OUT else SynthesisDisposition.CANCELLED
    failure = SynthesisFailure.DEADLINE_EXPIRED if cause is StopCause.TIMED_OUT else SynthesisFailure.CANCELLED
    if isinstance(request, SynthesisRequest):
        return SynthesisResult(
            disposition=disposition,
            purpose=request.purpose,
            backend=request.backend,
            engine=request.local.engine if request.backend is BackendId.LOCAL else None,
            configuration_generation=request.configuration_generation,
            content_identity=request.content_identity or "sha256:" + "0" * 64,
            preprocessing_version=request.preprocessing_version,
            failure=failure,
            source_identity=request.source_identity,
            event_identity=request.event_identity,
            segment_identity=request.segment_identity,
            elapsed_ms=max(0, min(86_400_000, int((time.monotonic() - started) * 1000))),
        )
    return type("AsyncStopResult", (), {"disposition": disposition, "failure": failure})()


def _failure_result(request: object, failure: SynthesisFailure, started: float) -> object:
    if isinstance(request, SynthesisRequest):
        return SynthesisResult(
            disposition=SynthesisDisposition.FAILED,
            purpose=request.purpose,
            backend=request.backend,
            engine=request.local.engine if request.backend is BackendId.LOCAL else None,
            configuration_generation=request.configuration_generation,
            content_identity=request.content_identity or "sha256:" + "0" * 64,
            preprocessing_version=request.preprocessing_version,
            failure=failure,
            elapsed_ms=max(0, min(86_400_000, int((time.monotonic() - started) * 1000))),
        )
    return type("AsyncFailureResult", (), {"disposition": SynthesisDisposition.FAILED, "failure": failure})()


def _effective_finalization_context(
    token: object, request: SynthesisRequest, reservation: object | None
) -> tuple[SynthesisRequest, object | None]:
    if isinstance(token, FinalizationContext):
        return token.request, token.capacity_reservation
    return request, reservation


def _normalize_stop_result(
    completion: object, request: object, stop: SynthesisStop, started: float, deadline: float
) -> object:
    result, completed_at, publication_decision_at = _completion_parts(completion)
    if _completion_finished_before_stop(completed_at, publication_decision_at, stop, deadline):
        # Worker-owned completion/publication evidence is authoritative.
        # Event-loop observation may be delayed past the deadline without
        # changing an already-completed typed result into a timeout.
        return result
    return _stop_result_for_cause(result, request, stop.cause, started)


def _completion_parts(completion: object) -> tuple[object, float | None, float | None]:
    if isinstance(completion, _WorkerCompletion):
        return completion.result, completion.completed_at, completion.publication_decision_at
    return completion, None, None


def _completion_finished_before_stop(
    completed_at: float | None,
    publication_decision_at: float | None,
    stop: SynthesisStop,
    deadline: float,
) -> bool:
    requested_at = stop.requested_at
    effective_at = publication_decision_at if publication_decision_at is not None else completed_at
    return (
        effective_at is not None and effective_at <= deadline and (requested_at is None or effective_at <= requested_at)
    )


def _stop_result_for_cause(result: object, request: object, cause: StopCause | None, started: float) -> object:
    if cause is None:
        return result
    disposition = getattr(result, "disposition", None)
    if cause is StopCause.TIMED_OUT and disposition is not SynthesisDisposition.TIMED_OUT:
        return _stop_result(request, cause, started)
    if cause is StopCause.CANCELLED and disposition not in {
        SynthesisDisposition.CANCELLED,
        SynthesisDisposition.TIMED_OUT,
    }:
        return _stop_result(request, cause, started)
    return result


def _capacity_reservation_required(tts: TTS, request: object, has_reservation_port: bool) -> bool:
    return bool(getattr(tts, "capacity_is_relevant", lambda _request: has_reservation_port)(request))


def _atomic_publication_replace(
    source: Path,
    target: Path,
    *,
    clock: Callable[[], float],
    deadline: float,
    stop: SynthesisStop,
    commit_decision: Callable[[float], None],
    clear_decision: Callable[[], None],
    abort_publication: Callable[[], None] | None = None,
) -> float:
    """Commit one valid decision, then perform only the atomic stable replace.

    The decision timestamp is the final deadline authority.  This function is
    called while the short bridge publication section is held.  Controller
    publication callbacks run after this function returns, outside that
    section.
    """

    try:
        decision_at = clock()
        cause = stop.cause
        if cause is StopCause.TIMED_OUT:
            raise ProcessFailure("timed_out", "TTS publication decision reached its deadline")
        if cause is StopCause.CANCELLED:
            raise ProcessFailure("cancelled", "TTS publication decision was cancelled")
        if decision_at >= deadline:
            raise ProcessFailure("timed_out", "TTS publication decision reached its deadline")
        if not stop.try_commit_publication():
            cause = stop.cause
            classification = "timed_out" if cause is StopCause.TIMED_OUT else "cancelled"
            raise ProcessFailure(classification, "TTS publication decision was stopped")
        commit_decision(decision_at)
        os.replace(source, target)
    except BaseException:
        stop.clear_publication_decision()
        clear_decision()
        if abort_publication is not None:
            abort_publication()
        raise
    return decision_at


def _call_synthesis(
    tts: TTS,
    request: object,
    worker_output: Path,
    stop: SynthesisStop,
    finalize: Finalize,
    capacity_reservation: object | None,
) -> object:
    """Call the modern port while retaining narrow test-double compatibility."""

    method = tts.synthesize_request
    kwargs: dict[str, object] = {"cancellation": stop}
    parameters = inspect.signature(method).parameters
    if "finalize" in parameters:
        kwargs["finalize"] = finalize
    if "capacity_reservation" in parameters:
        kwargs["capacity_reservation"] = capacity_reservation
    result = method(request, worker_output, **kwargs)  # type: ignore[arg-type]
    if "finalize" not in parameters and getattr(result, "disposition", None) in {
        SynthesisDisposition.SUCCEEDED,
        SynthesisDisposition.LKG_REUSED,
    }:
        # Narrow legacy test doubles may report typed success without owning a
        # raw file.  The completed-WAV compatibility operation performs the
        # authoritative raw-output check; the typed primitive preserves the
        # result boundary for these doubles.
        if worker_output.is_file():
            finalize(worker_output, stop, lambda: None)
    return result


async def _wait_for_worker(
    future: asyncio.Future[object],
    deadline: float,
    stop: SynthesisStop,
    timeout: float,
    cancel_if_queued: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> object | None:
    loop = asyncio.get_running_loop()
    while not future.done() and clock() < deadline:
        await asyncio.sleep(min(0.01, max(0.001, deadline - loop.time())))
    if future.done():
        return _completed_worker_result(future, stop, deadline, clock)
    return await _wait_for_worker_shutdown(future, stop, timeout, cancel_if_queued, loop)


def _completed_worker_result(
    future: asyncio.Future[object], stop: SynthesisStop, deadline: float, clock: Callable[[], float]
) -> object | None:
    completion = _completed_future_result(future, stop)
    _observed_at = clock()
    _result, completed_at, publication_decision_at = _completion_parts(completion)
    if (completed_at is None or completed_at > deadline) and (
        publication_decision_at is None or publication_decision_at > deadline
    ):
        stop.expire()
    return completion


async def _wait_for_worker_shutdown(
    future: asyncio.Future[object],
    stop: SynthesisStop,
    timeout: float,
    cancel_if_queued: Callable[[], None] | None,
    loop: asyncio.AbstractEventLoop,
) -> object | None:
    stop.expire()
    if cancel_if_queued is not None:
        cancel_if_queued()
    shutdown_deadline = loop.time() + max(0.01, timeout)
    while not future.done() and loop.time() < shutdown_deadline:
        await asyncio.sleep(min(0.01, max(0.001, shutdown_deadline - loop.time())))
    if future.done():
        try:
            return future.result()
        except (asyncio.CancelledError, ConcurrentCancelledError):
            if stop.cause is StopCause.TIMED_OUT:
                return None
            raise asyncio.CancelledError
    # A Python thread cannot be force-terminated.  The worker owns only the
    # private staging directory, so returning this typed timeout is safe;
    # deferred cleanup and capacity release are attached to the future.
    return None


def _completed_future_result(future: asyncio.Future[object], stop: SynthesisStop) -> object | None:
    try:
        return future.result()
    except (asyncio.CancelledError, ConcurrentCancelledError):
        if stop.cause is StopCause.TIMED_OUT:
            return None
        raise


async def synthesize_async(
    tts: TTS,
    text: str,
    output_path: Path,
    *,
    purpose: str,
    deadline_at: dt.datetime | None = None,
    shutdown_timeout: float = DEFAULT_SHUTDOWN_SECONDS,
    executor: Executor | None = None,
    finalize: Finalize | Callable[[object], FinalizationEvidence] | None = None,
    publication_fence: Callable[[], None] | None = None,
    publication_committed: Callable[[], None] | None = None,
    publication_aborted: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SynthesisResult:
    """Return typed synthesis state; explicit task cancellation stays native.

    The worker receives a private staging path.  A late non-cooperative worker
    can therefore only finish into deferred private cleanup, never recreate a
    caller-owned temporary or stable output path after this operation returns.
    """

    purpose_id = SynthesisPurpose(purpose)
    request_deadline = deadline_at or deadline_for(purpose_id, now=dt.datetime.now(dt.UTC))
    request = tts.request_for(text, purpose=purpose_id.value, deadline_at=request_deadline)
    started = clock()
    operation_deadline = started + max(0.0, (request_deadline - dt.datetime.now(dt.UTC)).total_seconds())
    if isinstance(request, SynthesisRequest):
        request = request.model_copy(update={"operation_deadline": operation_deadline})
    stop = SynthesisStop(clock=clock)
    publication_lock = threading.Lock()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=".tts-async-", dir=output_path.parent))
    worker_output = workspace / output_path.name
    reservation: object | None = None
    reservation_id = f"tts-{uuid.uuid4().hex}"
    publication_decision_at: float | None = None

    # Async callers wait for the controller-local P1-09 reservation before
    # occupying the embedded execution lane.  Test doubles without that port
    # retain the typed bridge behavior without inventing capacity truth.
    has_reservation_port = (
        callable(getattr(tts, "try_reserve_capacity", None))
        and getattr(getattr(tts, "capability_check", None), "reserve", None) is not None
    )
    needs_local_capacity = _capacity_reservation_required(tts, request, has_reservation_port)
    if has_reservation_port and needs_local_capacity:
        try:
            while reservation is None:
                if clock() >= operation_deadline:
                    stop.expire()
                    shutil.rmtree(workspace, ignore_errors=True)
                    return cast(SynthesisResult, _stop_result(request, StopCause.TIMED_OUT, started))
                try:
                    reservation = tts.try_reserve_capacity(
                        request,
                        reservation_id,
                        expires_at=request_deadline,
                    )
                except ProcessFailure as error:
                    failure = {
                        "capability_rejected": SynthesisFailure.CAPABILITY_REJECTED,
                        "timed_out": SynthesisFailure.DEADLINE_EXPIRED,
                        "cancelled": SynthesisFailure.CANCELLED,
                    }.get(error.classification, SynthesisFailure.BACKEND_UNAVAILABLE)
                    shutil.rmtree(workspace, ignore_errors=True)
                    return cast(SynthesisResult, _failure_result(request, failure, started))
                if reservation is None:
                    await asyncio.sleep(min(0.01, max(0.001, operation_deadline - clock())))
        except (asyncio.CancelledError, ConcurrentCancelledError):
            stop.cancel()
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    def finalization_fence(
        effective_request: SynthesisRequest = request,
        effective_reservation: object | None = reservation,
    ) -> object | None:
        checker = getattr(tts, "finalization_fence", None)
        authority = None
        if checker is not None:
            try:
                parameter_count = len(inspect.signature(checker).parameters)
            except (TypeError, ValueError):
                parameter_count = 0
            if parameter_count >= 3:
                authority = checker(effective_request, stop, effective_reservation)
            else:
                authority = checker(effective_request, stop)
            if getattr(authority, "qualified", True) is False:
                raise ProcessFailure("capability_rejected", "TTS capability changed before publication")
            if (
                hasattr(authority, "configuration_generation")
                and authority.configuration_generation != effective_request.configuration_generation
            ):
                raise ProcessFailure("stale_result", "configuration generation changed before publication")
        current_generation = getattr(tts, "current_generation", None)
        if current_generation is not None and not current_generation(effective_request.configuration_generation):
            raise ProcessFailure("stale_result", "configuration generation changed before publication")
        return authority

    def _clear_publication_decision() -> None:
        nonlocal publication_decision_at
        publication_decision_at = None

    def _set_publication_decision(decision_at: float) -> None:
        nonlocal publication_decision_at
        publication_decision_at = decision_at

    def guarded_finalize(worker_path: Path, token: object, fence: Callable[[], None]) -> FinalizationEvidence:
        nonlocal publication_decision_at
        if stop.cause is not None:
            if stop.cause is StopCause.TIMED_OUT:
                raise TimeoutError("TTS finalization reached its deadline")
            raise asyncio.CancelledError
        if finalize is None:
            if not worker_path.is_file() or worker_path.is_symlink():
                raise ValueError("TTS raw completion evidence is missing or unsafe")
            effective_request, effective_reservation = _effective_finalization_context(token, request, reservation)
            with publication_lock:
                finalization_fence(effective_request, effective_reservation)
                if publication_fence is not None:
                    publication_fence()
                # The worker-owned decision point is after the complete
                # authority fence and immediately before the one atomic
                # caller-visible replace.  The same timestamp both validates
                # the deadline and records the committed decision.
                publication_decision_at = _atomic_publication_replace(
                    worker_path,
                    output_path,
                    clock=clock,
                    deadline=operation_deadline,
                    stop=stop,
                    commit_decision=_set_publication_decision,
                    clear_decision=_clear_publication_decision,
                    abort_publication=publication_aborted,
                )
            if publication_committed is not None:
                try:
                    publication_committed()
                except BaseException:
                    if publication_aborted is not None:
                        publication_aborted()
                    raise
            return FinalizationEvidence(output_path)
        try:
            parameter_count = len(inspect.signature(finalize).parameters)
        except (TypeError, ValueError):
            parameter_count = 0
        evidence: object
        if parameter_count >= 3:
            evidence = cast(Finalize, finalize)(worker_path, token, fence)
        else:
            legacy_finalize = cast(Callable[[object], object], finalize)
            evidence = legacy_finalize(token)
        if not isinstance(evidence, FinalizationEvidence):
            raise ValueError("TTS finalizer must return typed private completion evidence")
        staged_path = evidence.staged_path
        workspace_root = worker_path.parent.resolve()
        try:
            staged_resolved = staged_path.resolve()
        except OSError as exc:
            raise ValueError("TTS finalizer completion evidence has an unsafe path") from exc
        if (
            staged_path.is_symlink()
            or not staged_path.is_file()
            or staged_resolved == output_path.resolve()
            or not staged_resolved.is_relative_to(workspace_root)
        ):
            raise ValueError("TTS finalizer completion evidence must identify one private staged file")
        # The finalizer has completed every potentially long operation.  This
        # is the only fence preceding the one bounded caller-visible write.
        effective_request, effective_reservation = _effective_finalization_context(token, request, reservation)
        with publication_lock:
            finalization_fence(effective_request, effective_reservation)
            if publication_fence is not None:
                publication_fence()
            publication_decision_at = _atomic_publication_replace(
                staged_path,
                output_path,
                clock=clock,
                deadline=operation_deadline,
                stop=stop,
                commit_decision=_set_publication_decision,
                clear_decision=_clear_publication_decision,
                abort_publication=publication_aborted,
            )
        if publication_committed is not None:
            try:
                publication_committed()
            except BaseException:
                if publication_aborted is not None:
                    publication_aborted()
                raise
        return FinalizationEvidence(output_path)

    def run() -> object:
        if stop.cause is not None:
            return _WorkerCompletion(
                result=_stop_result(request, stop.cause, started),
                completed_at=clock(),
            )
        result = _call_synthesis(tts, request, worker_output, stop, guarded_finalize, reservation)
        return _WorkerCompletion(
            result=result,
            completed_at=clock(),
            publication_decision_at=publication_decision_at,
        )

    selected_executor = executor or getattr(tts, "execution_executor", None)
    concurrent_future = None
    if selected_executor is None:
        # The event loop owns its shared default executor; no per-invocation
        # executor policy is created here.
        future = asyncio.get_running_loop().run_in_executor(None, run)
    else:
        concurrent_future = selected_executor.submit(run)
        future = asyncio.wrap_future(concurrent_future)

    cleanup_done = False

    def deferred_cleanup(_future: object) -> None:
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        if reservation is not None:
            tts.release_capacity(reservation)
        shutil.rmtree(workspace, ignore_errors=True)

    future.add_done_callback(deferred_cleanup)

    def cancel_queued() -> None:
        if concurrent_future is not None:
            concurrent_future.cancel()

    try:
        try:
            result = await _wait_for_worker(
                future,
                operation_deadline,
                stop,
                shutdown_timeout,
                cancel_if_queued=cancel_queued if concurrent_future is not None else None,
                clock=clock,
            )
        except asyncio.CancelledError:
            stop.cancel()
            try:
                await _wait_for_worker(
                    future,
                    clock(),
                    stop,
                    shutdown_timeout,
                    cancel_if_queued=cancel_queued if concurrent_future is not None else None,
                    clock=clock,
                )
            except BaseException:
                pass
            raise
        if result is None:
            return cast(SynthesisResult, _stop_result(request, StopCause.TIMED_OUT, started))
        return cast(SynthesisResult, _normalize_stop_result(result, request, stop, started, operation_deadline))
    finally:
        # If the worker has completed, cleanup runs through its callback.  A
        # still-running worker retains the private workspace until completion.
        if future.done():
            deferred_cleanup(future)


async def synthesize_completed_wav_async(
    tts: TTS,
    text: str,
    output_path: Path,
    *,
    purpose: str,
    deadline_at: dt.datetime | None = None,
    shutdown_timeout: float = DEFAULT_SHUTDOWN_SECONDS,
    executor: Executor | None = None,
    finalize: Finalize | Callable[[object], FinalizationEvidence] | None = None,
    publication_fence: Callable[[], None] | None = None,
    publication_committed: Callable[[], None] | None = None,
    publication_aborted: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SynthesisResult:
    """Compatibility operation for callers whose contract requires a WAV."""

    from .tts import TTSCompatibilityError

    result = await synthesize_async(
        tts,
        text,
        output_path,
        purpose=purpose,
        deadline_at=deadline_at,
        shutdown_timeout=shutdown_timeout,
        executor=executor,
        finalize=finalize,
        publication_fence=publication_fence,
        publication_committed=publication_committed,
        publication_aborted=publication_aborted,
        clock=clock,
    )
    if result.disposition not in {SynthesisDisposition.SUCCEEDED, SynthesisDisposition.LKG_REUSED}:
        raise TTSCompatibilityError(result)
    # A complete publication callback may promote the validated private
    # output into a controller-owned target before this compatibility check.
    # Without such a callback, the requested output path must still exist.
    if publication_committed is None and not Path(output_path).is_file():
        raise TTSCompatibilityError(result)
    return result
