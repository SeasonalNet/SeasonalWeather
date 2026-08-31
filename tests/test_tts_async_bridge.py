from __future__ import annotations

import asyncio
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from seasonalweather.tts.async_bridge import (
    EmbeddedExecutionPort,
    FinalizationEvidence,
    _WorkerCompletion,
    _atomic_publication_replace,
    _completed_worker_result,
    _normalize_stop_result,
    synthesize_async,
    synthesize_completed_wav_async,
)
from seasonalweather.tts.cancellation import SynthesisStop
from seasonalweather.tts.models import SynthesisDisposition, SynthesisFailure
from seasonalweather.tts.subprocess import ProcessFailure
from seasonalweather.tts.tts import TTSCompatibilityError


class FakeTTS:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.calls = 0
        self.cancellation = None
        self.started = threading.Event()
        self.release = threading.Event()

    def request_for(self, text: str, *, purpose: str, deadline_at: dt.datetime) -> object:
        request = SimpleNamespace(
            text=text,
            purpose=purpose,
            deadline_at=deadline_at,
            configuration_generation=4,
        )
        self.requests.append(request)
        return request

    def synthesize_request(self, request: object, output_path: Path, *, cancellation) -> object:
        del output_path
        self.calls += 1
        self.cancellation = cancellation
        self.started.set()
        while not cancellation.is_set() and not self.release.is_set():
            import time

            time.sleep(0.005)
        if cancellation.is_set():
            return SimpleNamespace(disposition=SynthesisDisposition.CANCELLED)
        return SimpleNamespace(disposition=SynthesisDisposition.SUCCEEDED)


def test_queue_delay_consumes_deadline_and_never_enters_tts(tmp_path: Path) -> None:
    asyncio.run(_test_queue_delay_consumes_deadline_and_never_enters_tts(tmp_path))


async def _test_queue_delay_consumes_deadline_and_never_enters_tts(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    release = threading.Event()
    blocker_started = threading.Event()
    blocker = executor.submit(lambda: (blocker_started.set(), release.wait()))
    for _ in range(20):
        if blocker_started.is_set():
            break
        await asyncio.sleep(0.005)
    assert blocker_started.is_set()
    tts = FakeTTS()
    operation = asyncio.create_task(
        synthesize_async(
            tts,
            "queued",
            tmp_path / "queued.wav",
            purpose="alert",
            deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(milliseconds=30),
            executor=executor,
        )
    )
    await asyncio.sleep(0.08)
    assert tts.calls == 0
    release.set()
    result = await operation
    assert result.disposition is SynthesisDisposition.TIMED_OUT
    executor.shutdown(wait=True, cancel_futures=True)


def test_async_cancellation_reaches_worker_and_preserves_cancelled_error(tmp_path: Path) -> None:
    asyncio.run(_test_async_cancellation_reaches_worker_and_preserves_cancelled_error(tmp_path))


async def _test_async_cancellation_reaches_worker_and_preserves_cancelled_error(tmp_path: Path) -> None:
    tts = FakeTTS()
    executor = EmbeddedExecutionPort()
    try:
        operation = asyncio.create_task(
            synthesize_async(tts, "cancel", tmp_path / "cancel.wav", purpose="routine", executor=executor)
        )
        assert await asyncio.to_thread(tts.started.wait, 1.0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert tts.calls == 1
    finally:
        executor.shutdown()


def test_running_deadline_is_typed_timeout_and_completion_before_boundary_survives(tmp_path: Path) -> None:
    async def run() -> None:
        timed_out_tts = FakeTTS()
        executor = EmbeddedExecutionPort()
        try:
            timed_out = await synthesize_async(
                timed_out_tts,
                "deadline",
                tmp_path / "deadline.wav",
                purpose="alert",
                deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(milliseconds=25),
                executor=executor,
            )
            assert timed_out.disposition is SynthesisDisposition.TIMED_OUT
        finally:
            executor.shutdown()

        completed_tts = FakeTTS()
        completed_tts.release.set()
        executor = EmbeddedExecutionPort()
        try:
            completed = await synthesize_async(
                completed_tts,
                "before deadline",
                tmp_path / "before-deadline.wav",
                purpose="alert",
                deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
                executor=executor,
            )
            assert completed.disposition is SynthesisDisposition.SUCCEEDED
        finally:
            executor.shutdown()

    asyncio.run(run())


def test_bridge_preserves_explicit_purpose_deadline(tmp_path: Path) -> None:
    asyncio.run(_test_bridge_preserves_explicit_purpose_deadline(tmp_path))


async def _test_bridge_preserves_explicit_purpose_deadline(tmp_path: Path) -> None:
    tts = FakeTTS()
    tts.release.set()
    executor = EmbeddedExecutionPort()
    try:
        await synthesize_async(tts, "admin", tmp_path / "admin.wav", purpose="administrative", executor=executor)
        request = tts.requests[0]
        seconds = (request.deadline_at - dt.datetime.now(dt.UTC)).total_seconds()
        assert request.purpose == "administrative"
        assert 170 < seconds <= 180
    finally:
        executor.shutdown()


@pytest.mark.parametrize(
    "disposition",
    (
        SynthesisDisposition.FAILED,
        SynthesisDisposition.SUPPRESSED,
        SynthesisDisposition.CANCELLED,
        SynthesisDisposition.TIMED_OUT,
    ),
)
def test_completed_wav_bridge_translates_every_non_output_disposition(tmp_path: Path, disposition) -> None:
    class ResultTTS(FakeTTS):
        def synthesize_request(self, request, output_path, *, cancellation):
            del request, output_path, cancellation
            return type("Result", (), {"disposition": disposition, "failure": SynthesisFailure.PROCESS_FAILED})()

    async def run() -> None:
        executor = EmbeddedExecutionPort()
        try:
            with pytest.raises(TTSCompatibilityError) as error:
                await synthesize_completed_wav_async(
                    ResultTTS(), "failure", tmp_path / f"{disposition.value}.wav", purpose="alert", executor=executor
                )
            assert error.value.result.disposition is disposition
        finally:
            executor.shutdown()

    asyncio.run(run())


def test_completed_wav_bridge_rejects_success_without_an_output(tmp_path: Path) -> None:
    tts = FakeTTS()
    tts.release.set()

    async def run() -> None:
        executor = EmbeddedExecutionPort()
        try:
            with pytest.raises(TTSCompatibilityError) as error:
                await synthesize_completed_wav_async(
                    tts, "missing", tmp_path / "missing.wav", purpose="routine", executor=executor
                )
            assert error.value.result.disposition is SynthesisDisposition.SUCCEEDED
        finally:
            executor.shutdown()

    asyncio.run(run())


def test_noncooperative_worker_cannot_recreate_caller_path_after_bounded_return(tmp_path: Path) -> None:
    class LateWriter(FakeTTS):
        def synthesize_request(self, request, output_path, *, cancellation):
            del request, cancellation
            import time

            time.sleep(0.15)
            output_path.write_bytes(b"late worker output")
            return type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()

    async def run() -> None:
        executor = EmbeddedExecutionPort()
        output = tmp_path / "caller-owned.wav"
        try:
            result = await synthesize_async(
                LateWriter(),
                "late",
                output,
                purpose="alert",
                deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(milliseconds=25),
                shutdown_timeout=0.01,
                executor=executor,
            )
            assert result.disposition is SynthesisDisposition.TIMED_OUT
            output.unlink(missing_ok=True)
            await asyncio.sleep(0.2)
            assert not output.exists()
        finally:
            executor.shutdown()
        assert not any(thread.name.startswith("seasonalweather-tts") for thread in threading.enumerate())

    asyncio.run(run())


def test_custom_finalizer_consumes_private_raw_artifact_and_publishes_explicit_completion(tmp_path: Path) -> None:
    def complete(staged_path, token, fence):
        del token, fence
        published = staged_path.parent / "caller-artifact.wav"
        published.write_bytes(b"finalized artifact")
        return FinalizationEvidence(published)

    class FinalizingTTS(FakeTTS):
        def synthesize_request(self, request, output_path, *, cancellation, finalize):
            del request
            output_path.write_bytes(b"raw intermediate")
            output_path.unlink()
            finalize(output_path, cancellation, lambda: None)
            return type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()

    async def run() -> None:
        executor = EmbeddedExecutionPort()
        output = tmp_path / "published.wav"
        try:
            result = await synthesize_completed_wav_async(
                FinalizingTTS(), "consumed", output, purpose="alert", executor=executor, finalize=complete
            )
            assert result.disposition is SynthesisDisposition.SUCCEEDED
            assert output.read_bytes() == b"finalized artifact"
        finally:
            executor.shutdown()

    asyncio.run(run())


def test_slow_finalizer_cannot_publish_after_deadline(tmp_path: Path) -> None:
    def complete(staged_path, token, fence):
        del token, fence
        import time

        time.sleep(0.08)
        published = staged_path.parent / "too-late.wav"
        published.write_bytes(b"too late")
        return FinalizationEvidence(published)

    class SlowFinalizingTTS(FakeTTS):
        def synthesize_request(self, request, output_path, *, cancellation, finalize):
            del request
            output_path.write_bytes(b"raw")
            finalize(output_path, cancellation, lambda: None)
            return type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()

    async def run() -> None:
        executor = EmbeddedExecutionPort()
        output = tmp_path / "deadline-finalized.wav"
        try:
            result = await synthesize_async(
                SlowFinalizingTTS(),
                "slow finalization",
                output,
                purpose="alert",
                deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(milliseconds=20),
                shutdown_timeout=0.01,
                executor=executor,
                finalize=complete,
            )
            assert result.disposition is SynthesisDisposition.TIMED_OUT
            assert not output.exists()
            await asyncio.sleep(0.1)
            assert not output.exists()
        finally:
            executor.shutdown(wait=True)

    asyncio.run(run())


def test_explicit_cancellation_during_finalization_cannot_publish_later(tmp_path: Path) -> None:
    finalizer_started = threading.Event()

    def complete(staged_path, token, fence):
        del token, fence
        import time

        finalizer_started.set()
        time.sleep(0.1)
        published = staged_path.parent / "cancelled-too-late.wav"
        published.write_bytes(b"cancelled")
        return FinalizationEvidence(published)

    class CancellableFinalizingTTS(FakeTTS):
        def synthesize_request(self, request, output_path, *, cancellation, finalize):
            del request
            output_path.write_bytes(b"raw")
            finalize(output_path, cancellation, lambda: None)
            return type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()

    async def run() -> None:
        executor = EmbeddedExecutionPort()
        output = tmp_path / "cancelled-finalized.wav"
        operation = asyncio.create_task(
            synthesize_async(
                CancellableFinalizingTTS(),
                "cancel finalization",
                output,
                purpose="routine",
                shutdown_timeout=0.01,
                executor=executor,
                finalize=complete,
            )
        )
        try:
            assert await asyncio.to_thread(finalizer_started.wait, 1.0)
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 1)
            await asyncio.sleep(0.15)
            assert not output.exists()
        finally:
            executor.shutdown(wait=False)

    asyncio.run(run())


def test_authority_checker_returning_after_deadline_cannot_publish(tmp_path: Path) -> None:
    async def run() -> None:
        checker_started = threading.Event()
        checker_release = threading.Event()
        clock_state = [0.0]

        class CheckerTTS(FakeTTS):
            def synthesize_request(self, request, output_path, *, cancellation, finalize):
                del request
                output_path.write_bytes(b"raw")
                try:
                    finalize(output_path, cancellation, lambda: None)
                except Exception:
                    return type("Result", (), {"disposition": SynthesisDisposition.TIMED_OUT})()
                return type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()

            def finalization_fence(self, request, cancellation, reservation=None):
                del request, cancellation, reservation
                checker_started.set()
                assert checker_release.wait(1)

        executor = EmbeddedExecutionPort()
        output = tmp_path / "checker-too-late.wav"
        try:
            operation = asyncio.create_task(
                synthesize_async(
                    CheckerTTS(),
                    "checker",
                    output,
                    purpose="alert",
                    deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
                    executor=executor,
                    clock=lambda: clock_state[0],
                )
            )
            while not checker_started.is_set():
                await asyncio.sleep(0)
            clock_state[0] = 2.0
            checker_release.set()
            result = await operation
            assert result.disposition is SynthesisDisposition.TIMED_OUT
            assert not output.exists()
        finally:
            executor.shutdown(wait=True)

    asyncio.run(run())


def test_worker_publication_before_deadline_survives_late_future_observation(tmp_path: Path) -> None:
    async def run() -> None:
        published = threading.Event()
        release_return = threading.Event()
        clock_state = [0.0]

        class DelayedObservationTTS(FakeTTS):
            def synthesize_request(self, request, output_path, *, cancellation, finalize):
                del request, cancellation
                output_path.write_bytes(b"published")
                finalize(output_path, object(), lambda: None)
                published.set()
                assert release_return.wait(1)
                return type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()

        executor = EmbeddedExecutionPort()
        output = tmp_path / "published-before-deadline.wav"
        try:
            operation = asyncio.create_task(
                synthesize_async(
                    DelayedObservationTTS(),
                    "published",
                    output,
                    purpose="alert",
                    deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
                    executor=executor,
                    clock=lambda: clock_state[0],
                )
            )
            while not published.is_set():
                await asyncio.sleep(0)
            clock_state[0] = 2.0
            release_return.set()
            result = await operation
            assert result.disposition is SynthesisDisposition.SUCCEEDED
            assert output.read_bytes() == b"published"
        finally:
            executor.shutdown(wait=True)

    asyncio.run(run())


def test_atomic_replace_crossing_deadline_keeps_committed_publication_success(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        clock_state = [0.0]

        class CrossingReplaceTTS(FakeTTS):
            def synthesize_request(self, request, output_path, *, cancellation, finalize):
                del request, cancellation
                output_path.write_bytes(b"published")
                finalize(output_path, object(), lambda: None)
                return type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()

        original_replace = __import__("seasonalweather.tts.async_bridge", fromlist=["os"]).os.replace

        def crossing_replace(source, target):
            clock_state[0] = 2.0
            original_replace(source, target)

        monkeypatch.setattr("seasonalweather.tts.async_bridge.os.replace", crossing_replace)
        executor = EmbeddedExecutionPort()
        output = tmp_path / "crossing.wav"
        try:
            result = await synthesize_async(
                CrossingReplaceTTS(),
                "replace crosses deadline",
                output,
                purpose="alert",
                deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
                executor=executor,
                clock=lambda: clock_state[0],
            )
            assert result.disposition is SynthesisDisposition.SUCCEEDED
            assert output.read_bytes() == b"published"
        finally:
            executor.shutdown(wait=True)

    asyncio.run(run())


def test_failed_atomic_replace_clears_publication_evidence(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        class FailingReplaceTTS(FakeTTS):
            def synthesize_request(self, request, output_path, *, cancellation, finalize):
                del request, cancellation
                output_path.write_bytes(b"private")
                try:
                    finalize(output_path, object(), lambda: None)
                except OSError:
                    return type(
                        "Result",
                        (),
                        {"disposition": SynthesisDisposition.FAILED, "failure": SynthesisFailure.OUTPUT_INVALID},
                    )()
                raise AssertionError("replace unexpectedly succeeded")

        def fail_replace(_source, _target):
            raise OSError("synthetic replace failure")

        monkeypatch.setattr("seasonalweather.tts.async_bridge.os.replace", fail_replace)
        executor = EmbeddedExecutionPort()
        output = tmp_path / "replace-failed.wav"
        try:
            result = await synthesize_async(
                FailingReplaceTTS(),
                "replace failure",
                output,
                purpose="alert",
                executor=executor,
            )
            assert result.disposition is SynthesisDisposition.FAILED
            assert not output.exists()
        finally:
            executor.shutdown(wait=True)

    asyncio.run(run())


def test_publication_decision_timestamp_rejects_generation_check_that_crosses_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    async def run() -> None:
        clock_state = [0.0]
        replace_calls = 0

        class GenerationRaceTTS(FakeTTS):
            def synthesize_request(self, request, output_path, *, cancellation, finalize):
                del request
                output_path.write_bytes(b"published")
                try:
                    finalize(output_path, cancellation, lambda: None)
                except ProcessFailure as error:
                    assert error.classification == "timed_out"
                    return type("Result", (), {"disposition": SynthesisDisposition.TIMED_OUT})()
                raise AssertionError("publication decision unexpectedly succeeded")

            def current_generation(self, generation):
                del generation
                clock_state[0] = 2.0
                return True

        def forbidden_replace(_source, _target):
            nonlocal replace_calls
            replace_calls += 1
            raise AssertionError("deadline-crossing publication reached os.replace")

        monkeypatch.setattr("seasonalweather.tts.async_bridge.os.replace", forbidden_replace)
        executor = EmbeddedExecutionPort()
        output = tmp_path / "generation-race.wav"
        try:
            result = await synthesize_async(
                GenerationRaceTTS(),
                "generation race",
                output,
                purpose="alert",
                deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
                executor=executor,
                clock=lambda: clock_state[0],
            )
        finally:
            executor.shutdown(wait=True)
        assert result.disposition is SynthesisDisposition.TIMED_OUT
        assert replace_calls == 0
        assert not output.exists()

    asyncio.run(run())


def test_final_generation_failure_blocks_publication_as_stale(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        replace_calls = 0

        class StaleGenerationTTS(FakeTTS):
            def synthesize_request(self, request, output_path, *, cancellation, finalize):
                del request
                output_path.write_bytes(b"stale")
                try:
                    finalize(output_path, cancellation, lambda: None)
                except ProcessFailure as error:
                    assert error.classification == "stale_result"
                    return type("Result", (), {"disposition": SynthesisDisposition.FAILED})()
                raise AssertionError("stale publication unexpectedly succeeded")

            def current_generation(self, generation):
                del generation
                return False

        def forbidden_replace(_source, _target):
            nonlocal replace_calls
            replace_calls += 1
            raise AssertionError("stale publication reached os.replace")

        monkeypatch.setattr("seasonalweather.tts.async_bridge.os.replace", forbidden_replace)
        executor = EmbeddedExecutionPort()
        output = tmp_path / "stale-generation.wav"
        try:
            result = await synthesize_async(
                StaleGenerationTTS(),
                "stale generation",
                output,
                purpose="routine",
                executor=executor,
            )
        finally:
            executor.shutdown(wait=True)
        assert result.disposition is SynthesisDisposition.FAILED
        assert replace_calls == 0
        assert not output.exists()

    asyncio.run(run())


def test_stop_request_cannot_overtake_committed_publication_decision(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        cancellation_token: list[object] = []
        cancellation_attempted = threading.Event()
        cancellation_thread: list[threading.Thread] = []
        original_replace = __import__("seasonalweather.tts.async_bridge", fromlist=["os"]).os.replace

        class CommittedPublicationTTS(FakeTTS):
            def synthesize_request(self, request, output_path, *, cancellation, finalize):
                del request
                cancellation_token.append(cancellation)
                output_path.write_bytes(b"committed")
                finalize(output_path, cancellation, lambda: None)
                return type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()

        def replace_while_stop_waits(source, target):
            def request_stop():
                cancellation_attempted.set()
                cancellation_token[0].cancel()

            thread = threading.Thread(target=request_stop)
            cancellation_thread.append(thread)
            thread.start()
            assert cancellation_attempted.wait(1)
            original_replace(source, target)

        monkeypatch.setattr("seasonalweather.tts.async_bridge.os.replace", replace_while_stop_waits)
        executor = EmbeddedExecutionPort()
        output = tmp_path / "committed-before-stop.wav"
        try:
            result = await synthesize_async(
                CommittedPublicationTTS(),
                "committed publication",
                output,
                purpose="alert",
                executor=executor,
                clock=lambda: 0.0,
            )
        finally:
            executor.shutdown(wait=True)
        cancellation_thread[0].join(1)
        assert not cancellation_thread[0].is_alive()
        assert result.disposition is SynthesisDisposition.SUCCEEDED
        assert output.read_bytes() == b"committed"

    asyncio.run(run())


def test_atomic_publication_replace_revokes_decision_on_replace_failure(tmp_path: Path) -> None:
    source = tmp_path / "private.wav"
    target = tmp_path / "stable.wav"
    source.write_bytes(b"private")
    stop = SynthesisStop(clock=lambda: 0.0)
    committed: list[float] = []
    revoked: list[None] = []

    def fail_replace(_source, _target):
        raise OSError("synthetic replace failure")

    import seasonalweather.tts.async_bridge as async_bridge

    original_replace = async_bridge.os.replace
    async_bridge.os.replace = fail_replace
    try:
        with pytest.raises(OSError, match="synthetic replace failure"):
            _atomic_publication_replace(
                source,
                target,
                clock=lambda: 0.0,
                deadline=1.0,
                stop=stop,
                commit_decision=committed.append,
                clear_decision=lambda: revoked.append(None),
            )
    finally:
        async_bridge.os.replace = original_replace
    assert committed == [0.0]
    assert revoked == [None]
    assert not target.exists()


@pytest.mark.parametrize("disposition", (SynthesisDisposition.FAILED, SynthesisDisposition.SUPPRESSED))
def test_pre_deadline_nonpublication_result_survives_late_observation(disposition) -> None:
    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        result = type("Result", (), {"disposition": disposition})()
        future.set_result(_WorkerCompletion(result=result, completed_at=0.25))
        stop = SynthesisStop(clock=lambda: 2.0)
        observed = _completed_worker_result(future, stop, 1.0, lambda: 2.0)
        assert observed is not None
        assert _normalize_stop_result(observed, object(), stop, 0.0, 1.0) is result
        assert stop.cause is None
    finally:
        loop.close()


def test_successful_publication_decision_survives_late_observation() -> None:
    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        result = type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()
        future.set_result(_WorkerCompletion(result=result, completed_at=2.0, publication_decision_at=0.25))
        stop = SynthesisStop(clock=lambda: 2.0)
        observed = _completed_worker_result(future, stop, 1.0, lambda: 2.0)
        assert observed is not None
        assert _normalize_stop_result(observed, object(), stop, 0.0, 1.0) is result
        assert stop.cause is None
    finally:
        loop.close()


def test_deadline_before_publication_leaves_no_output(tmp_path: Path) -> None:
    async def run() -> None:
        clock_state = [0.0]

        class ExpiredTTS(FakeTTS):
            def synthesize_request(self, request, output_path, *, cancellation, finalize):
                del request
                output_path.write_bytes(b"raw")
                clock_state[0] = 2.0
                try:
                    finalize(output_path, cancellation, lambda: None)
                except Exception:
                    return type("Result", (), {"disposition": SynthesisDisposition.TIMED_OUT})()
                return type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()

        executor = EmbeddedExecutionPort()
        output = tmp_path / "expired.wav"
        try:
            result = await synthesize_async(
                ExpiredTTS(),
                "expired",
                output,
                purpose="alert",
                deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
                executor=executor,
                clock=lambda: clock_state[0],
            )
            assert result.disposition is SynthesisDisposition.TIMED_OUT
            assert not output.exists()
        finally:
            executor.shutdown(wait=True)

    asyncio.run(run())


def test_cancellation_wins_immediately_before_final_replace(tmp_path: Path) -> None:
    async def run() -> None:
        finalizer_ready = threading.Event()
        finalizer_release = threading.Event()
        cancellation_token: list[object] = []

        def complete(staged_path, token, fence):
            del fence
            cancellation_token.append(token)
            finalizer_ready.set()
            assert finalizer_release.wait(1)
            private_output = staged_path.parent / "cancelled-private.wav"
            private_output.write_bytes(b"cancelled")
            return FinalizationEvidence(private_output)

        class CancelBeforeReplaceTTS(FakeTTS):
            def synthesize_request(self, request, output_path, *, cancellation, finalize):
                del request
                output_path.write_bytes(b"raw")
                try:
                    finalize(output_path, cancellation, lambda: None)
                except Exception:
                    return type("Result", (), {"disposition": SynthesisDisposition.CANCELLED})()
                return type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()

        executor = EmbeddedExecutionPort()
        output = tmp_path / "cancelled.wav"
        try:
            operation = asyncio.create_task(
                synthesize_async(
                    CancelBeforeReplaceTTS(),
                    "cancelled",
                    output,
                    purpose="routine",
                    executor=executor,
                    finalize=complete,
                )
            )
            while not finalizer_ready.is_set():
                await asyncio.sleep(0)
            cancellation_token[0].cancel()
            finalizer_release.set()
            result = await operation
            assert result.disposition is SynthesisDisposition.CANCELLED
            assert not output.exists()
        finally:
            executor.shutdown(wait=True)

    asyncio.run(run())


def test_exact_deadline_boundary_is_rejected_deterministically(tmp_path: Path) -> None:
    async def one(index: int) -> None:
        clock_state = [0.0]

        class BoundaryTTS(FakeTTS):
            def synthesize_request(self, request, output_path, *, cancellation, finalize):
                del request
                output_path.write_bytes(b"raw")
                try:
                    finalize(output_path, cancellation, lambda: None)
                except Exception:
                    return type("Result", (), {"disposition": SynthesisDisposition.TIMED_OUT})()
                return type("Result", (), {"disposition": SynthesisDisposition.SUCCEEDED})()

            def finalization_fence(self, request, cancellation, reservation=None):
                del request, cancellation, reservation
                clock_state[0] = 1.0

        executor = EmbeddedExecutionPort()
        output = tmp_path / f"boundary-{index}.wav"
        try:
            result = await synthesize_async(
                BoundaryTTS(),
                "boundary",
                output,
                purpose="alert",
                deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
                executor=executor,
                clock=lambda: clock_state[0],
            )
            assert result.disposition is SynthesisDisposition.TIMED_OUT
            assert not output.exists()
        finally:
            executor.shutdown(wait=True)

    async def run() -> None:
        for index in range(20):
            await one(index)

    asyncio.run(run())
