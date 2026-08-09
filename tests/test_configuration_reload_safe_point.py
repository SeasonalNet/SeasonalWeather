from __future__ import annotations

import asyncio
import threading

import pytest

from seasonalweather.configuration_reload.safe_point import (
    ALERT,
    CONDUCTOR,
    PUBLICATION,
    SEGMENT_REFRESH,
    TTS,
    WORKER_RESULT,
    ActivityRegistry,
    SafePointCoordinator,
    SafePointTimeout,
    SynchronousActivityAdmissionBlocked,
)


def test_alert_has_priority_while_reload_waits() -> None:
    async def scenario() -> None:
        registry = ActivityRegistry()
        coordinator = SafePointCoordinator(registry, poll_interval_seconds=0.001)
        alert_started = asyncio.Event()
        release_alert = asyncio.Event()

        async def alert() -> None:
            async with registry.async_activity(ALERT):
                alert_started.set()
                await release_alert.wait()

        task = asyncio.create_task(alert())
        await alert_started.wait()
        reload_task = asyncio.create_task(coordinator.acquire(0.2))
        await asyncio.sleep(0)
        assert not reload_task.done()
        release_alert.set()
        await task
        lease = await reload_task
        lease.release()

    asyncio.run(scenario())


def test_safe_point_timeout_and_cancellation_leave_gate_reusable() -> None:
    async def scenario() -> None:
        registry = ActivityRegistry()
        coordinator = SafePointCoordinator(registry, poll_interval_seconds=0.001)
        with registry.activity(TTS):
            with pytest.raises(SafePointTimeout) as raised:
                await coordinator.acquire(0.01)
            assert raised.value.snapshot.blockers == (TTS,)
        task = asyncio.create_task(coordinator.acquire(1.0))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lease = await coordinator.acquire(0.1)
        lease.release()

    asyncio.run(scenario())


@pytest.mark.parametrize("category", (CONDUCTOR, PUBLICATION, SEGMENT_REFRESH, TTS, WORKER_RESULT))
def test_declared_routine_activity_drains_before_safe_point(category: str) -> None:
    async def scenario() -> None:
        registry = ActivityRegistry()
        coordinator = SafePointCoordinator(registry, poll_interval_seconds=0.001)
        with registry.activity(category):
            pending = asyncio.create_task(coordinator.acquire(0.2))
            await asyncio.sleep(0)
            assert not pending.done()
        lease = await pending
        lease.release()

    asyncio.run(scenario())


def test_commit_gate_blocks_new_synchronous_activity_until_release() -> None:
    async def scenario() -> None:
        registry = ActivityRegistry()
        coordinator = SafePointCoordinator(registry, poll_interval_seconds=0.001)
        lease = await coordinator.acquire(0.2)
        started = threading.Event()

        def synthesize() -> None:
            with registry.activity(TTS):
                started.set()

        thread = threading.Thread(target=synthesize, daemon=True)
        thread.start()
        try:
            await asyncio.sleep(0.01)
            assert not started.is_set()
        finally:
            lease.release()
        thread.join(timeout=0.2)
        assert started.is_set()
        assert not thread.is_alive()

    asyncio.run(scenario())


def test_same_event_loop_synchronous_activity_fails_fast_during_commit_gate() -> None:
    async def scenario() -> None:
        registry = ActivityRegistry()
        coordinator = SafePointCoordinator(registry, poll_interval_seconds=0.001)
        lease = await coordinator.acquire(0.2)
        try:
            with pytest.raises(SynchronousActivityAdmissionBlocked):
                with registry.activity(TTS):
                    pass
        finally:
            lease.release()

    asyncio.run(scenario())
