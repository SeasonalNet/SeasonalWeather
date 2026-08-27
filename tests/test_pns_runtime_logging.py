from types import SimpleNamespace
from zoneinfo import ZoneInfo

from seasonalweather.broadcast.formatters import PnsStateMachine
from seasonalweather.broadcast.pns_runtime import PnsRuntime


def _runtime() -> PnsRuntime:
    def _state_machine(cfg, *, tz):
        return PnsStateMachine(cfg, tz=tz)

    host = SimpleNamespace(
        cfg=SimpleNamespace(pns=SimpleNamespace()),
        _tz=ZoneInfo("America/New_York"),
        formatters=SimpleNamespace(
            pns_state_machine=_state_machine,
        ),
    )
    return PnsRuntime(host)


def test_pns_repeatable_log_throttle_collapses_within_window() -> None:
    runtime = _runtime()

    first, repeats = runtime._should_log_repeatable("api-backfill:suppressed:example", now=100.0)
    assert first is True
    assert repeats == 0

    second, repeats = runtime._should_log_repeatable("api-backfill:suppressed:example", now=220.0)
    assert second is False
    assert repeats == 1

    third, repeats = runtime._should_log_repeatable("api-backfill:suppressed:example", now=400.0)
    assert third is False
    assert repeats == 2


def test_pns_repeatable_log_throttle_reports_suppressed_count_after_window() -> None:
    runtime = _runtime()

    assert runtime._should_log_repeatable("api-backfill:dedupe:example", now=100.0) == (True, 0)
    assert runtime._should_log_repeatable("api-backfill:dedupe:example", now=200.0) == (False, 1)
    assert runtime._should_log_repeatable("api-backfill:dedupe:example", now=300.0) == (False, 2)

    should_log, repeats = runtime._should_log_repeatable("api-backfill:dedupe:example", now=2000.0)
    assert should_log is True
    assert repeats == 2
