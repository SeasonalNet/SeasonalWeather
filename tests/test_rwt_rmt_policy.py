from __future__ import annotations

import datetime as dt

from seasonalweather.broadcast.rwt_rmt import _next_postpone_attempt
from seasonalweather.broadcast.tests import (
    default_test_script_lines,
    format_test_presentation_template,
    normalize_postpone_policy,
)


def test_postpone_policy_names_are_normalized() -> None:
    assert normalize_postpone_policy("next-day") == "next_day"
    assert normalize_postpone_policy("delay_window") == "delay_window"
    assert normalize_postpone_policy("bad", "next_day") == "next_day"


def test_next_day_policy_advances_by_calendar_days() -> None:
    scheduled = dt.datetime(2026, 4, 29, 11, 0, tzinfo=dt.timezone.utc)
    now = scheduled
    assert _next_postpone_attempt(
        policy="next_day",
        scheduled=scheduled,
        now=now,
        blocked_count=0,
        postpone=dt.timedelta(minutes=15),
        deadline=scheduled,
        max_postpone_days=2,
    ) == dt.datetime(2026, 4, 30, 11, 0, tzinfo=dt.timezone.utc)

    assert _next_postpone_attempt(
        policy="next_day",
        scheduled=scheduled,
        now=now,
        blocked_count=2,
        postpone=dt.timedelta(minutes=15),
        deadline=scheduled,
        max_postpone_days=2,
    ) is None


def test_delay_window_policy_respects_deadline() -> None:
    scheduled = dt.datetime(2026, 4, 29, 11, 0, tzinfo=dt.timezone.utc)
    now = scheduled + dt.timedelta(minutes=20)
    assert _next_postpone_attempt(
        policy="delay_window",
        scheduled=scheduled,
        now=now,
        blocked_count=0,
        postpone=dt.timedelta(minutes=15),
        deadline=scheduled + dt.timedelta(hours=1),
        max_postpone_days=0,
    ) == dt.datetime(2026, 4, 29, 11, 35, tzinfo=dt.timezone.utc)

    assert _next_postpone_attempt(
        policy="delay_window",
        scheduled=scheduled,
        now=scheduled + dt.timedelta(minutes=55),
        blocked_count=0,
        postpone=dt.timedelta(minutes=15),
        deadline=scheduled + dt.timedelta(hours=1),
        max_postpone_days=0,
    ) is None


def test_default_test_scripts_are_outside_main() -> None:
    rwt = default_test_script_lines("RWT")
    rmt = default_test_script_lines("RMT")
    assert any("weekly test" in line.lower() for line in rwt)
    assert any("monthly test" in line.lower() for line in rmt)
    assert all("End of message." not in line for line in rwt + rmt)


def test_default_test_scripts_use_configured_station_identity() -> None:
    lines = default_test_script_lines(
        "RWT",
        organization_name="Example Broadcaster",
        service_name="Weather Radio Service",
        station_name="Example Station",
    )
    assert lines[0] == "This is the Example Broadcaster Weather Radio Service, Example Station."
    assert "Example Broadcaster" in lines[-1]


def test_presentation_template_falls_back_to_literal_on_bad_template() -> None:
    assert format_test_presentation_template("{event}", event="Required Weekly Test") == "Required Weekly Test"
    assert format_test_presentation_template("{missing", event="Required Weekly Test") == "{missing"
