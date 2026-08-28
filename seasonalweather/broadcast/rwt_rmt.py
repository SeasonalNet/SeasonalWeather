from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..database.core import SeasonalDatabase
from ..database.scheduler import SchedulerStateRepository
from .tests import normalize_postpone_policy


@dataclass(frozen=True)
class RwtRmtSchedule:
    enabled: bool
    tz_name: str

    rwt_enabled: bool
    rwt_weekday: int  # 0=Mon..6=Sun
    rwt_hour: int
    rwt_minute: int

    rmt_enabled: bool
    rmt_nth: int  # 1..5
    rmt_weekday: int  # 0=Mon..6=Sun
    rmt_hour: int
    rmt_minute: int

    jitter_seconds: int
    postpone_minutes: int
    max_postpone_hours: int

    state_path: str
    state_key: str = "rwt_rmt"

    # Per-test postpone policy. This lets RWTs use NWS-style next-day
    # behavior while RMTs keep a tighter retry window.
    rwt_postpone_policy: str = "next_day"
    rwt_postpone_minutes: int = 15
    rwt_max_postpone_hours: int = 6
    rwt_max_postpone_days: int = 2

    rmt_postpone_policy: str = "delay_window"
    rmt_postpone_minutes: int = 15
    rmt_max_postpone_hours: int = 6
    rmt_max_postpone_days: int = 0


@dataclass
class TestState:
    last_rwt_period: str = ""
    last_rmt_period: str = ""

    @staticmethod
    def _payload_from_obj(obj: object) -> TestState:
        if not isinstance(obj, dict):
            return TestState()
        return TestState(
            last_rwt_period=str(obj.get("last_rwt_period", "")),
            last_rmt_period=str(obj.get("last_rmt_period", "")),
        )

    @staticmethod
    def load(
        path: str,
        *,
        repository: SchedulerStateRepository | None = None,
        state_key: str = "rwt_rmt",
    ) -> TestState:
        if repository is None:
            return TestState()
        try:
            row = repository.get_state(state_key)
            if row is not None:
                return TestState._payload_from_obj(row.get("state") or {})
            with open(path, encoding="utf-8") as handle:
                state = TestState._payload_from_obj(json.load(handle) or {})
            repository.upsert_state(
                state_key,
                state={"last_rwt_period": state.last_rwt_period, "last_rmt_period": state.last_rmt_period},
            )
            os.unlink(path)
            return state
        except Exception:
            return TestState()

    def save(
        self,
        path: str,
        *,
        repository: SchedulerStateRepository | None = None,
        state_key: str = "rwt_rmt",
        last_run_at: str | None = None,
        next_run_at: str | None = None,
    ) -> None:
        del path
        payload = {
            "last_rwt_period": self.last_rwt_period,
            "last_rmt_period": self.last_rmt_period,
        }
        if repository is None:
            return
        try:
            repository.upsert_state(
                state_key,
                last_run_at=last_run_at,
                next_run_at=next_run_at,
                state=payload,
            )
        except Exception:
            return


def _weekly_period_key(now: dt.datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _monthly_period_key(now: dt.datetime) -> str:
    return f"{now.year}-{now.month:02d}"


def _next_weekly(now: dt.datetime, weekday: int, hour: int, minute: int) -> dt.datetime:
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (weekday - base.weekday()) % 7
    if days_ahead == 0 and base <= now:
        days_ahead = 7
    return base + dt.timedelta(days=days_ahead)


def _days_in_month(y: int, m: int) -> int:
    nxt = dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1)
    return (nxt - dt.date(y, m, 1)).days


def _nth_weekday_of_month(y: int, m: int, nth: int, weekday: int) -> dt.date | None:
    if nth < 1 or nth > 5:
        return None
    first = dt.date(y, m, 1)
    delta = (weekday - first.weekday()) % 7
    day = 1 + delta + (nth - 1) * 7
    if day > _days_in_month(y, m):
        return None
    return dt.date(y, m, day)


def _next_monthly_nth_weekday(now: dt.datetime, nth: int, weekday: int, hour: int, minute: int) -> dt.datetime:
    y, m = now.year, now.month
    for _ in range(0, 15):
        d = _nth_weekday_of_month(y, m, nth, weekday)
        if d is not None:
            cand = dt.datetime(y, m, d.day, hour, minute, 0, 0, tzinfo=now.tzinfo)
            if cand > now:
                return cand
        # advance month
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    return now + dt.timedelta(days=30)


GateFn = Callable[[str], tuple[bool, str]]
FireFn = Callable[[str], Awaitable[None]]
LogFn = Callable[[str], None]


def _next_postpone_attempt(
    *,
    policy: str,
    scheduled: dt.datetime,
    now: dt.datetime,
    blocked_count: int,
    postpone: dt.timedelta,
    deadline: dt.datetime,
    max_postpone_days: int,
) -> dt.datetime | None:
    policy = normalize_postpone_policy(policy)
    if policy in {"none", "skip_day", "skip_week"}:
        return None
    if policy == "fixed_delay":
        if blocked_count >= 1:
            return None
        candidate = now + postpone
        return candidate if candidate <= deadline else None
    if policy == "delay_window":
        candidate = now + postpone
        return candidate if candidate <= deadline else None
    if policy == "next_day":
        if blocked_count >= max(0, int(max_postpone_days)):
            return None
        return scheduled + dt.timedelta(days=blocked_count + 1)
    return None


class RwtRmtScheduler:
    def __init__(
        self,
        schedule: RwtRmtSchedule,
        gate_fn: GateFn,
        fire_fn: FireFn,
        log_fn: LogFn,
        *,
        database: SeasonalDatabase | None = None,
    ):
        self.s = schedule
        self.gate_fn = gate_fn
        self.fire_fn = fire_fn
        self.log = log_fn
        self.tz = ZoneInfo(self.s.tz_name)
        self._repo = SchedulerStateRepository(database) if database is not None else None
        self.state = TestState.load(self.s.state_path, repository=self._repo, state_key=self.s.state_key)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def _now(self) -> dt.datetime:
        return dt.datetime.now(self.tz)

    def _jitter(self) -> dt.timedelta:
        j = int(self.s.jitter_seconds)
        if j <= 0:
            return dt.timedelta(0)
        return dt.timedelta(seconds=random.uniform(0, float(j)))

    def _postpone_policy_for(self, event_code: str) -> str:
        if str(event_code or "").strip().upper() == "RWT":
            return normalize_postpone_policy(self.s.rwt_postpone_policy, "next_day")
        return normalize_postpone_policy(self.s.rmt_postpone_policy, "delay_window")

    def _postpone_minutes_for(self, event_code: str) -> int:
        if str(event_code or "").strip().upper() == "RWT":
            return max(1, int(self.s.rwt_postpone_minutes))
        return max(1, int(self.s.rmt_postpone_minutes))

    def _max_postpone_hours_for(self, event_code: str) -> int:
        if str(event_code or "").strip().upper() == "RWT":
            return max(0, int(self.s.rwt_max_postpone_hours))
        return max(0, int(self.s.rmt_max_postpone_hours))

    def _max_postpone_days_for(self, event_code: str) -> int:
        if str(event_code or "").strip().upper() == "RWT":
            return max(0, int(self.s.rwt_max_postpone_days))
        return max(0, int(self.s.rmt_max_postpone_days))

    def _gate(self, event_code: str) -> tuple[bool, str]:
        try:
            return self.gate_fn(event_code)
        except TypeError:
            # Compatibility for older no-argument gate callbacks.
            return self.gate_fn()  # type: ignore[misc,call-arg]

    async def _sleep_until(self, when: dt.datetime) -> None:
        while not self._stop.is_set():
            now = self._now()
            if now >= when:
                return
            await asyncio.sleep(min(30.0, max(1.0, (when - now).total_seconds())))

    async def run_forever(self) -> None:
        if not self.s.enabled:
            self.log("[RWT/RMT] disabled")
            return

        self.log("[RWT/RMT] scheduler started")

        while not self._stop.is_set():
            now = self._now()

            next_events: list[tuple[str, dt.datetime]] = []

            if self.s.rwt_enabled:
                due = _next_weekly(now, self.s.rwt_weekday, self.s.rwt_hour, self.s.rwt_minute) + self._jitter()
                next_events.append(("RWT", due))

            if self.s.rmt_enabled:
                due = (
                    _next_monthly_nth_weekday(
                        now, self.s.rmt_nth, self.s.rmt_weekday, self.s.rmt_hour, self.s.rmt_minute
                    )
                    + self._jitter()
                )
                next_events.append(("RMT", due))

            if not next_events:
                await asyncio.sleep(5)
                continue

            event_code, due = min(
                next_events, key=lambda x: (x[1], 0 if x[0] == "RMT" else 1)
            )  # PATCH: prefer RMT on tie
            self.log(f"[RWT/RMT] next={event_code} at {due.isoformat()}")
            self.state.save(
                self.s.state_path,
                repository=self._repo,
                state_key=self.s.state_key,
                next_run_at=due.astimezone(dt.UTC).replace(microsecond=0).isoformat(),
            )

            # wait in chunks so stop is responsive
            while not self._stop.is_set():
                now = self._now()
                if now >= due:
                    break
                await asyncio.sleep(min(30.0, max(1.0, (due - now).total_seconds())))

            if self._stop.is_set():
                break

            # de-dupe per period
            if event_code == "RWT":
                period = _weekly_period_key(due)
                if self.state.last_rwt_period == period:
                    self.log(f"[RWT/RMT] skip {event_code}: already ran for {period}")
                    continue
            else:
                period = _monthly_period_key(due)
                if self.state.last_rmt_period == period:
                    self.log(f"[RWT/RMT] skip {event_code}: already ran for {period}")
                    continue

            await self._attempt_with_postpone(event_code, due)

        self.log("[RWT/RMT] scheduler stopped")

    async def _attempt_with_postpone(self, event_code: str, scheduled: dt.datetime) -> None:
        policy = self._postpone_policy_for(event_code)
        postpone = dt.timedelta(minutes=self._postpone_minutes_for(event_code))
        deadline = scheduled + dt.timedelta(hours=self._max_postpone_hours_for(event_code))
        max_postpone_days = self._max_postpone_days_for(event_code)
        blocked_count = 0
        attempt_at = scheduled

        while not self._stop.is_set():
            await self._sleep_until(attempt_at)
            if self._stop.is_set():
                return

            now = self._now()
            ok, reason = self._gate(event_code)
            if ok:
                break

            next_attempt = _next_postpone_attempt(
                policy=policy,
                scheduled=scheduled,
                now=now,
                blocked_count=blocked_count,
                postpone=postpone,
                deadline=deadline,
                max_postpone_days=max_postpone_days,
            )
            blocked_count += 1

            if next_attempt is None:
                self.log(f"[RWT/RMT] skip {event_code}: gate blocked ({reason}); policy={policy}")
                return

            self.log(
                f"[RWT/RMT] gate blocked for {event_code}: {reason} (policy={policy} next={next_attempt.isoformat()})"
            )
            self.state.save(
                self.s.state_path,
                repository=self._repo,
                state_key=self.s.state_key,
                next_run_at=next_attempt.astimezone(dt.UTC).replace(microsecond=0).isoformat(),
            )
            attempt_at = next_attempt

        if self._stop.is_set():
            return

        self.log(f"[RWT/RMT] firing {event_code}")
        await self.fire_fn(event_code)

        completed_at = self._now().astimezone(dt.UTC).replace(microsecond=0).isoformat()
        if event_code == "RWT":
            self.state.last_rwt_period = _weekly_period_key(self._now())
        else:
            self.state.last_rmt_period = _monthly_period_key(self._now())
        self.state.save(
            self.s.state_path,
            repository=self._repo,
            state_key=self.s.state_key,
            last_run_at=completed_at,
            next_run_at=None,
        )
        self.log(f"[RWT/RMT] completed {event_code}")
