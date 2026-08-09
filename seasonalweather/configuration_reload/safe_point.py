"""Explicit bounded safe-point coordination with alert-priority admission."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from threading import Condition, Lock
from typing import Any

from seasonalweather.diagnostics.bindings import RELOAD_CODES

from .models import SafePointSnapshot

ALERT = "alert_origination"
PUBLICATION = "artifact_publication"
TTS = "tts_synthesis"
WORKER_RESULT = "worker_result_promotion"
SEGMENT_REFRESH = "segment_refresh"
CONDUCTOR = "conductor_mutation"
LIFECYCLE = "lifecycle_drain"
_KNOWN = frozenset({ALERT, PUBLICATION, TTS, WORKER_RESULT, SEGMENT_REFRESH, CONDUCTOR, LIFECYCLE})


class SafePointTimeout(TimeoutError):
    diagnostic_code = RELOAD_CODES["safe_point_timeout"]

    def __init__(self, snapshot: SafePointSnapshot) -> None:
        self.snapshot = snapshot
        super().__init__("configuration reload safe point timed out")


class SynchronousActivityAdmissionBlocked(RuntimeError):
    """A synchronous caller attempted new work during the held commit fence."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"reload commit gate excludes new synchronous {category} activity")


class ActivityRegistry:
    """Shared counters plus a narrow async gate held only across final commit."""

    def __init__(self) -> None:
        self._counts = {name: 0 for name in _KNOWN}
        self._counter_lock = Lock()
        self._condition = Condition(self._counter_lock)
        self._gate = asyncio.Lock()
        self._alert_waiters = 0
        self._commit_active = False

    @contextmanager
    def activity(self, category: str) -> Iterator[None]:
        self._begin_synchronous_activity(category)
        try:
            yield
        finally:
            self._change(category, -1)

    @asynccontextmanager
    async def async_activity(self, category: str):
        if category == ALERT:
            with self._counter_lock:
                self._alert_waiters += 1
        try:
            async with self._gate:
                if category == ALERT:
                    with self._counter_lock:
                        self._alert_waiters -= 1
                self._change(category, 1)
                try:
                    yield
                finally:
                    self._change(category, -1)
        except BaseException:
            if category == ALERT:
                with self._counter_lock:
                    if self._alert_waiters > 0:
                        self._alert_waiters -= 1
            raise

    def blockers(self) -> tuple[str, ...]:
        with self._counter_lock:
            values = [name for name, count in self._counts.items() if count > 0]
            if self._alert_waiters > 0 and ALERT not in values:
                values.append(ALERT)
        return tuple(sorted(values))

    def alert_waiting(self) -> bool:
        with self._counter_lock:
            return self._alert_waiters > 0 or self._counts[ALERT] > 0

    async def acquire_gate(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._gate.acquire(), timeout=timeout)
        except TimeoutError:
            return False
        with self._condition:
            if self._alert_waiters > 0 or any(self._counts.values()):
                self._gate.release()
                return False
            self._commit_active = True
        return True

    def release_gate(self) -> None:
        with self._condition:
            self._commit_active = False
            self._condition.notify_all()
        self._gate.release()

    def _begin_synchronous_activity(self, category: str) -> None:
        if category not in _KNOWN:
            raise ValueError("unknown reload activity category")
        with self._condition:
            while self._commit_active:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    raise SynchronousActivityAdmissionBlocked(category)
                self._condition.wait()
            self._counts[category] += 1

    def _change(self, category: str, delta: int) -> None:
        if category not in _KNOWN:
            raise ValueError("unknown reload activity category")
        with self._counter_lock:
            updated = self._counts[category] + delta
            if updated < 0:
                raise RuntimeError("reload activity counter underflow")
            self._counts[category] = updated


@dataclass
class SafePointLease:
    registry: ActivityRegistry
    snapshot: SafePointSnapshot
    _released: bool = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self.registry.release_gate()

    async def __aenter__(self) -> SafePointLease:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.release()


class SafePointCoordinator:
    """Waits for all declared owners, then returns a held immediate-commit gate."""

    def __init__(
        self,
        registry: ActivityRegistry,
        *,
        external_blockers: Callable[[], tuple[str, ...]] = lambda: (),
        monotonic: Callable[[], float] = time.monotonic,
        poll_interval_seconds: float = 0.01,
    ) -> None:
        if not 0.001 <= poll_interval_seconds <= 1.0:
            raise ValueError("safe-point polling interval is outside its bound")
        self.registry = registry
        self._external_blockers = external_blockers
        self._monotonic = monotonic
        self._poll_interval = poll_interval_seconds

    def blockers(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.registry.blockers()) | set(self._external_blockers())))[:16]

    async def acquire(
        self,
        timeout_seconds: float,
        *,
        abort: Callable[[], Awaitable[None]] | None = None,
    ) -> SafePointLease:
        started = self._monotonic()
        deadline = started + float(timeout_seconds)
        last = self.blockers()
        while self._monotonic() < deadline:
            if abort is not None:
                await abort()
            last = self.blockers()
            if not last and not self.registry.alert_waiting():
                if abort is not None:
                    await abort()
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break
                acquired = await self.registry.acquire_gate(min(self._poll_interval, remaining))
                if not acquired:
                    continue
                after = self.blockers()
                if not after and not self.registry.alert_waiting():
                    return SafePointLease(
                        self.registry,
                        SafePointSnapshot((), max(0.0, self._monotonic() - started)),
                    )
                self.registry.release_gate()
                last = after
            await asyncio.sleep(min(self._poll_interval, max(0.0, deadline - self._monotonic())))
        raise SafePointTimeout(SafePointSnapshot(last, max(0.0, self._monotonic() - started)))


def orchestrator_blockers(orch: Any) -> tuple[str, ...]:
    lifecycle = getattr(orch, "lifecycle", None)
    publication = getattr(orch, "publication_fence", None)
    cycle_lock = getattr(orch, "_cycle_lock", None)
    conductor = getattr(orch, "conductor", None)
    dispatcher = getattr(orch, "alert_audio", None)
    checks = (
        (LIFECYCLE, bool(lifecycle and getattr(lifecycle, "is_shutting_down", False))),
        (PUBLICATION, bool(publication and getattr(publication, "active", False))),
        (CONDUCTOR, bool(cycle_lock and cycle_lock.locked())),
        (ALERT, bool(conductor and getattr(conductor, "_interrupt_hold", False))),
        (ALERT, bool(dispatcher and dispatcher.pending_count())),
    )
    return tuple(sorted({category for category, active in checks if active}))
