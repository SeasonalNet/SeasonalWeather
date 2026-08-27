"""
broadcast/conductor.py — CycleConductor: continuous broadcast cycle driver.

Design
------
The conductor maintains a soft real-time estimate of how much audio is
buffered ahead in Liquidsoap's cycle queue.  Every *tick* it pushes the
next segment(s) until the estimated buffer depth exceeds *lookahead_s*.

Key differences from the old architecture
------------------------------------------
- No "outro" segment.  The cycle wraps from obs back to id continuously,
  just like a real NWR BMH.
- No repeat multiplier.  Segments are pushed one at a time as the buffer
  drains, so the queue depth stays bounded and stays fresh.
- The "time" segment is synthesised at push time (never cached) so the
  spoken time is always within a few seconds of accurate.
- Interrupt admission places the conductor on hold while Liquidsoap owns the
  output.  Once both alert planes are idle, the routine cycle is reset and
  rebuilt from station ID/current time instead of resuming stale audio.
- Alert-tracker voice segments are injected right after "time" each
  rotation in operational-priority order, without re-toning.
- In focus mode, routine low-priority segments are deferred instead of
  forcing heightened-mode text truncation.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncContextManager
from zoneinfo import ZoneInfo

from ..alerts.active import AlertTracker
from ..alerts.focus import AlertFocusPolicy, alert_holds_focus
from ..diagnostics.bindings import FOUNDATION_CODES
from ..liquidsoap_telnet import LiquidsoapTelnet
from ..jobs.worker_client import SynthesisClient
from ..tts.audio import concat_wavs, wav_duration_seconds, write_silence_wav
from .segment_registry import DEFAULT_SEGMENT_REGISTRY, ResolvedSegmentRegistry, SegmentBuilderKind
from .segment_store import SegmentStore, segment_entry_eligible_to_air

log = logging.getLogger("seasonalweather.conductor")

# How many seconds of audio to keep ahead in Liquidsoap's cycle queue.
_LOOKAHEAD_S: float = 75.0

# How often the conductor checks the buffer level (seconds).
_TICK_S: float = 4.0

# Silence padding each side of the live time segment.
_SEG_GAP_S: float = 0.45

# Number of time WAV buffers to rotate through (avoids overwriting a
# file that Liquidsoap may still have queued).
_TIME_BUF_COUNT: int = 2

# Maximum segments to push in a single tick (safety valve).
_MAX_PUSHES_PER_TICK: int = 30

# Once the expected interrupt duration has elapsed, poll Liquidsoap at this
# cadence until both interrupt queues are actually idle.  Duration-based wakeup
# avoids opening a telnet connection every few hundred milliseconds throughout
# long alerts, while the final status check covers queued/preempted alerts.
_INTERRUPT_STATUS_POLL_S: float = 0.5


def focus_mode_from_inputs(alerts: Iterable[Any], policy: AlertFocusPolicy, mode: object) -> bool:
    """Pure focus projection shared by mutation and read-only inspection."""
    try:
        if any(alert_holds_focus(alert, policy) for alert in alerts):
            return True
    except Exception:
        pass
    try:
        return str(mode or "").strip().lower() == "heightened"
    except Exception:
        return False


def deferred_focus_projection(
    registry: ResolvedSegmentRegistry,
    *,
    now: float,
    last_pushed_at: dict[str, float],
    focus: bool,
    previously_focused: bool,
) -> tuple[dict[str, float], tuple[str, ...]]:
    """Purely project focus-entry timer reset and deferred due order."""
    projected = dict(last_pushed_at)
    if focus and not previously_focused:
        for key in registry.deferred_focus_keys():
            projected[key] = now
    due = tuple(
        key
        for key in registry.deferred_focus_keys()
        if focus and now - projected.get(key, now) >= registry.minimum_air_interval(key)
    )
    return projected, due


@dataclass(frozen=True)
class CycleOrderProjection:
    """One pure, internally consistent projection of the next rotation."""

    registry: ResolvedSegmentRegistry
    focus: bool
    projected_timers: dict[str, float]
    due_deferred_keys: tuple[str, ...]
    static_order: tuple[str, ...]
    alert_order: tuple[str, ...]
    scheduled_insert_placements: tuple[tuple[str, tuple[str, ...]], ...]
    order: tuple[str, ...]
    runtime_items: tuple[dict[str, Any], ...]
    insert_cache: dict[str, dict[str, Any]]


# ---------------------------------------------------------------------------
#  Time formatting (mirrors cycle.py — kept local to avoid a circular import)
# ---------------------------------------------------------------------------

_TZ_NAME_MAP: dict[str, str] = {
    "EST": "Eastern Standard Time",
    "EDT": "Eastern Daylight Time",
    "CST": "Central Standard Time",
    "CDT": "Central Daylight Time",
    "MST": "Mountain Standard Time",
    "MDT": "Mountain Daylight Time",
    "PST": "Pacific Standard Time",
    "PDT": "Pacific Daylight Time",
    "AKST": "Alaska Standard Time",
    "AKDT": "Alaska Daylight Time",
    "HST": "Hawaii Standard Time",
    "AST": "Atlantic Standard Time",
    "ADT": "Atlantic Daylight Time",
    "UTC": "Coordinated Universal Time",
    "GMT": "Greenwich Mean Time",
}


def _fmt_time(now: dt.datetime) -> str:
    return now.strftime("%-I:%M %p")


def _short_tz(now: dt.datetime) -> str:
    tok = (now.tzname() or "").strip()
    return _TZ_NAME_MAP.get(tok.upper(), tok or "local")


def _rotation_now_iso(now: float) -> str:
    return dt.datetime.fromtimestamp(now, dt.UTC).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
#  CycleConductor
# ---------------------------------------------------------------------------


class CycleConductor:
    """
    Continuously feeds Liquidsoap's cycle queue one segment at a time.

    The conductor tracks an estimated buffer depth and pushes the next
    segment whenever depth falls below *lookahead_s*.  This produces a
    continuous, ever-rolling broadcast with no outgoing silence gaps and
    no need for the old repeat-count arithmetic.
    """

    def __init__(
        self,
        *,
        store: SegmentStore,
        telnet: LiquidsoapTelnet,
        tts: SynthesisClient,
        alert_tracker: AlertTracker,
        tz: ZoneInfo,
        audio_dir: Path,
        sample_rate: int,
        np_meta_fn: Callable[..., dict[str, str]],
        registry: ResolvedSegmentRegistry | None = None,
        seg_gap_s: float = _SEG_GAP_S,
        lookahead_s: float = _LOOKAHEAD_S,
        tick_s: float = _TICK_S,
        discord_fn: Callable[..., Any] | None = None,
        active_alerts_fn: Callable[[], int] | None = None,
        mode_fn: Callable[[], str] | None = None,
        alert_focus_policy: AlertFocusPolicy | None = None,
        scheduled_inserts_fn: Callable[[str, int, bool, str], list[dict[str, Any]]] | None = None,
        scheduled_inserts_snapshot_fn: Callable[[str, int, bool, str], list[dict[str, Any]]] | None = None,
        mark_insert_aired_fn: Callable[[str, int], Any] | None = None,
        mark_segment_aired_fn: Callable[[str], Any] | None = None,
        activity_context: Callable[[], AsyncContextManager[None]] | None = None,
        diagnostic_sink: object | None = None,
    ) -> None:
        """
        Parameters
        ----------
        store:
            The shared SegmentStore (populated/maintained by SegmentRefresher).
        telnet:
            Live Liquidsoap telnet client.
        tts:
            Worker synthesis client used for the live time segment.
        alert_tracker:
            AlertTracker for injecting active-alert voice segments.
        tz:
            Station timezone (used for the live time announcement).
        audio_dir:
            Directory for time-segment WAV buffers.
        sample_rate:
            PCM sample rate that matches the rest of the audio chain.
        np_meta_fn:
            ``Orchestrator._np_meta`` callable — builds Now-Playing metadata.
        seg_gap_s:
            Silence padding each side of the live time WAV.
        lookahead_s:
            Target buffer depth to maintain in Liquidsoap (seconds).
        tick_s:
            Conductor poll interval (seconds).
        """
        self._store = store
        self._telnet = telnet
        self._synthesizer = tts
        self._tts = tts
        self._alert_tracker = alert_tracker
        self._registry = registry or DEFAULT_SEGMENT_REGISTRY.resolve()
        self._tz = tz
        self._audio_dir = Path(audio_dir)
        self._sample_rate = sample_rate
        self._np_meta_fn = np_meta_fn
        self._discord_fn = discord_fn  # optional: fires cycle_rebuilt embed
        self._active_alerts_fn = active_alerts_fn  # optional: count for embed
        self._mode_fn = mode_fn
        self._alert_focus_policy = alert_focus_policy or AlertFocusPolicy()
        self._scheduled_inserts_fn = scheduled_inserts_fn
        self._scheduled_inserts_snapshot_fn = scheduled_inserts_snapshot_fn
        self._mark_insert_aired_fn = mark_insert_aired_fn
        self._mark_segment_aired_fn = mark_segment_aired_fn
        self._activity_context = activity_context
        self._diagnostic_sink: object | None = diagnostic_sink
        self._seg_gap_s = seg_gap_s
        self._lookahead_s = lookahead_s
        self._tick_s = tick_s

        # Buffer accounting
        self._push_start_ts: float = time.time()
        self._total_pushed_s: float = 0.0

        # Cycle position tracking
        self._position_in_rotation: int = 0
        self._cycle_order: list[str] = []  # rebuilt at each rotation start
        self._last_cycle_order: list[str] = []
        self._insert_cache: dict[str, dict[str, Any]] = {}
        self._last_pushed_at: dict[str, float] = {}
        self._focus_mode_active: bool = False

        # Double-buffer for live time WAV (prevents overwriting while queued)
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._time_bufs: list[Path] = [self._audio_dir / f"cycle_time_{i}.wav" for i in range(_TIME_BUF_COUNT)]
        self._time_buf_idx: int = 0

        # Flush notification event (set by notify_flush to wake the loop early)
        self._flush_event: asyncio.Event = asyncio.Event()

        # Interrupt hold.  The cycle queue is safely reset after alert admission,
        # then the conductor stops feeding it until all admitted interrupt audio
        # has had time to play and Liquidsoap confirms both alert planes are idle.
        self._interrupt_hold: bool = False
        self._interrupt_expected_end: float = 0.0
        self._interrupt_reason: str = ""

        # Rotation accounting (for Discord embed + heartbeat log)
        self._rotation_count: int = 0
        self._rotation_seg_count: int = 0
        self._rotation_start_ts: float = time.time()

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def notify_flush(self, *, reset_rotation: bool = True, reason: str = "") -> None:
        """
        Call this after any explicit cycle queue reset.  Resets buffer tracking
        so the conductor refills the cycle queue as fast as possible.  By
        default, also resets
        rotation order so newly active alerts are heard by priority instead of
        inheriting an old chronological slot.
        """
        self._push_start_ts = time.time()
        self._total_pushed_s = 0.0
        if reset_rotation:
            self._cycle_order = []
            self._position_in_rotation = 0
        self._flush_event.set()
        log.debug(
            "CycleConductor: notify_flush — buffer reset, reset_rotation=%s reason=%s",
            reset_rotation,
            reason or "-",
        )

    def notify_inserts_changed(self) -> None:
        """Wake the conductor so newly scheduled inserts are considered promptly."""
        self._flush_event.set()
        log.debug("CycleConductor: scheduled inserts changed — waking loop")

    def notify_interrupt_started(self, *, duration_s: float, reason: str = "") -> None:
        """Pause cycle production while interrupt audio owns the output.

        Each newly admitted alert extends the expected end deadline.  FULL and
        VOICE sources are mutually exclusive in the fallback graph, so their
        on-air durations are additive even when one preempts the other.
        """
        duration = max(0.1, float(duration_s))
        now = time.monotonic()
        self._interrupt_expected_end = max(now, self._interrupt_expected_end) + duration
        self._interrupt_hold = True
        self._interrupt_reason = reason or self._interrupt_reason or "interrupt"

        # The Liquidsoap queue has just been reset.  Reset local accounting too,
        # but do not refill until the hold is released.
        self._push_start_ts = time.time()
        self._total_pushed_s = 0.0
        self._cycle_order = []
        self._position_in_rotation = 0
        self._flush_event.set()
        log.info(
            "CycleConductor: interrupt hold started/extended duration=%.1fs expected_remaining=%.1fs reason=%s",
            duration,
            max(0.0, self._interrupt_expected_end - now),
            self._interrupt_reason,
        )

    @property
    def estimated_remaining_s(self) -> float:
        """Estimated seconds of audio buffered ahead in Liquidsoap."""
        consumed = time.time() - self._push_start_ts
        return max(0.0, self._total_pushed_s - consumed)

    def inspection_snapshot(self) -> dict[str, Any]:
        """Return one immutable runtime/order projection without mutation."""
        now = time.time()
        try:
            alerts = tuple(self._alert_tracker.get_cycle_alerts())
        except Exception:
            alerts = ()
        try:
            mode = self._mode_fn() if self._mode_fn else ""
        except Exception:
            mode = ""
        projection = self._project_next_rotation(
            now=now,
            alerts=alerts,
            mode=mode,
            rotation_count=self._rotation_count + 1,
            read_only=True,
        )
        due_set = set(projection.due_deferred_keys)
        deferred: list[dict[str, Any]] = []
        for key in self._registry.deferred_focus_keys():
            entry = self._store.get(key)
            provenance = entry.provenance if entry is not None else None
            deferred.append(
                {
                    "key": key,
                    "due": key in due_set,
                    "last_aired": provenance.last_aired if provenance is not None else None,
                    "next_eligible": provenance.next_eligible_airtime if provenance is not None else None,
                }
            )
        return {
            "mode": mode,
            "focus": projection.focus,
            "deferred": tuple(deferred),
            "deferred_keys": tuple(item["key"] for item in deferred),
            "deferred_due_keys": tuple(item["key"] for item in deferred if item["due"]),
            "order": projection.order,
            "runtime_items": projection.runtime_items,
            "projected_timers": tuple(sorted(projection.projected_timers.items())),
            # Internal-only identity consumed by SegmentApplicationService;
            # it is removed before any public response is returned.
            "_registry": projection.registry,
        }

    # ------------------------------------------------------------------
    #  Main async loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        log.info(
            "CycleConductor: starting (lookahead=%.0fs tick=%.1fs)",
            self._lookahead_s,
            self._tick_s,
        )
        while True:
            if self._interrupt_hold:
                await self._wait_for_interrupt_end()
                continue

            try:
                pushes = 0
                while self.estimated_remaining_s < self._lookahead_s:
                    pushed = await self._tracked_push_next_segment()
                    pushes += 1
                    if not pushed:
                        # Nothing was ready — don't busy-spin.
                        await asyncio.sleep(1.0)
                        break
                    if pushes >= _MAX_PUSHES_PER_TICK:
                        break
            except Exception:
                log.exception("CycleConductor: unhandled error in push loop")

            # Sleep until next tick or any flush/state notification wakes the
            # conductor early.
            self._flush_event.clear()
            try:
                await asyncio.wait_for(self._flush_event.wait(), timeout=self._tick_s)
            except TimeoutError:
                pass

    async def _wait_for_interrupt_end(self) -> None:
        """Hold cycle production until expected audio ends and queues are idle."""
        now = time.monotonic()
        remaining = self._interrupt_expected_end - now
        if remaining > 0.0:
            self._flush_event.clear()
            try:
                await asyncio.wait_for(self._flush_event.wait(), timeout=remaining)
            except TimeoutError:
                pass
            return

        active: bool | None = None
        try:
            active = self._telnet.interrupt_active()
        except AttributeError:
            active = None
        except Exception as exc:
            # A failed state query must not prematurely return stale cycle audio.
            log.warning(
                "CycleConductor: interrupt status query failed; retaining cycle hold",
                exc_info=True,
            )
            self._diagnose_liquidsoap(
                FOUNDATION_CODES["liquidsoap.control_failed"],
                "Liquidsoap interrupt state could not be queried.",
                exc,
            )
            active = True

        if active is True:
            self._interrupt_expected_end = time.monotonic() + _INTERRUPT_STATUS_POLL_S
            return

        # Re-clear at release to catch a cycle push that was already rendering
        # when the interrupt began.  The guarded alias skips only when a current
        # cycle request exists, so an empty source cannot acquire a pending skip.
        reset_ok = False
        try:
            reset_ok = bool(self._telnet.reset_cycle_safely())
        except AttributeError:
            reset_ok = False
        except Exception:
            log.exception("CycleConductor: final safe cycle reset failed")
            self._diagnose_liquidsoap(
                FOUNDATION_CODES["liquidsoap.publication_reconciliation"],
                "Liquidsoap cycle reset failed while releasing an interrupt hold.",
            )

        self._interrupt_hold = False
        self._interrupt_expected_end = 0.0
        reason = self._interrupt_reason or "interrupt-ended"
        self._interrupt_reason = ""
        self.notify_flush(reset_rotation=True, reason=f"{reason}:ended")
        log.info(
            "CycleConductor: interrupt hold released; cycle reset=%s and refill requested",
            reset_ok,
        )

    async def _tracked_push_next_segment(self) -> bool:
        if self._activity_context is None:
            return await self._push_next_segment()
        async with self._activity_context():
            return await self._push_next_segment()

    # ------------------------------------------------------------------
    #  Segment push orchestration
    # ------------------------------------------------------------------

    def _scheduled_insert_items_for(
        self,
        placement: str,
        *,
        rotation_count: int,
        focus: bool,
        read_only: bool,
        now_iso: str,
    ) -> list[dict[str, Any]]:
        callback = self._scheduled_inserts_snapshot_fn if read_only else self._scheduled_inserts_fn
        if callback is None:
            return []
        items: list[dict[str, Any]] = []
        try:
            for item in callback(placement, rotation_count, focus, now_iso) or []:
                insert_id = str(item.get("insert_id") or "").strip()
                audio_path = str(item.get("audio_path") or "").strip()
                if not insert_id or not audio_path:
                    continue
                items.append(dict(item))
        except Exception:
            log.exception("CycleConductor: could not fetch scheduled inserts placement=%s", placement)
        return items

    def _project_alert_items(self, alerts: tuple[Any, ...]) -> tuple[list[str], list[dict[str, Any]]]:
        keys: list[str] = []
        runtime_items: list[dict[str, Any]] = []
        for alert in alerts:
            alert_id = str(getattr(alert, "id", "")).strip()
            if not alert_id:
                continue
            key = f"_alert_{alert_id}"
            keys.append(key)
            entry = self._store.get(key)
            runtime_items.append(
                {
                    "key": key,
                    "kind": "alert",
                    "alert_id": alert_id,
                    "title": entry.title if entry is not None else "Active alert",
                    "ready": segment_entry_eligible_to_air(entry),
                }
            )
        return keys, runtime_items

    def _project_insert_items(
        self,
        placement: str,
        *,
        rotation_count: int,
        focus: bool,
        read_only: bool,
        now_iso: str,
    ) -> tuple[list[str], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        keys: list[str] = []
        insert_cache: dict[str, dict[str, Any]] = {}
        runtime_items: list[dict[str, Any]] = []
        for item in self._scheduled_insert_items_for(
            placement,
            rotation_count=rotation_count,
            focus=focus,
            read_only=read_only,
            now_iso=now_iso,
        ):
            insert_id = str(item["insert_id"]).strip()
            key = f"_insert_{insert_id}"
            keys.append(key)
            if not read_only:
                insert_cache[key] = item
            runtime_items.append(
                {
                    "key": key,
                    "kind": "scheduled_insert",
                    "insert_id": insert_id,
                    "title": str(item.get("title") or "Scheduled announcement."),
                    "placement": placement,
                    "ready": Path(str(item.get("audio_path") or "")).is_file(),
                }
            )
        return keys, insert_cache, runtime_items

    def _project_cycle_order(
        self,
        *,
        now: float,
        focus: bool,
        rotation_count: int,
        alerts: tuple[Any, ...],
        read_only: bool,
        now_iso: str,
        static_order: tuple[str, ...] | None = None,
        due_deferred_keys: tuple[str, ...] = (),
    ) -> tuple[list[str], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Project the next order without changing conductor state."""
        order: list[str] = []
        insert_cache: dict[str, dict[str, Any]] = {}
        runtime_items: list[dict[str, Any]] = []

        for static_key in static_order if static_order is not None else self._registry.static_order(focus=focus):
            order.append(static_key)
            runtime_items.append({"key": static_key, "kind": "static"})
            if static_key == "time":
                alert_keys, alert_items = self._project_alert_items(alerts)
                order.extend(alert_keys)
                runtime_items.extend(alert_items)
                insert_keys, inserts, insert_items = self._project_insert_items(
                    "after_time",
                    rotation_count=rotation_count,
                    focus=focus,
                    read_only=read_only,
                    now_iso=now_iso,
                )
                order.extend(insert_keys)
                insert_cache.update(inserts)
                runtime_items.extend(insert_items)
            elif static_key == "status":
                insert_keys, inserts, insert_items = self._project_insert_items(
                    "after_status",
                    rotation_count=rotation_count,
                    focus=focus,
                    read_only=read_only,
                    now_iso=now_iso,
                )
                order.extend(insert_keys)
                insert_cache.update(inserts)
                runtime_items.extend(insert_items)

        if focus:
            for key in due_deferred_keys:
                if key not in order:
                    order.append(key)
                    runtime_items.append({"key": key, "kind": "static"})

        insert_keys, inserts, insert_items = self._project_insert_items(
            "end_of_rotation",
            rotation_count=rotation_count,
            focus=focus,
            read_only=read_only,
            now_iso=now_iso,
        )
        order.extend(insert_keys)
        insert_cache.update(inserts)
        runtime_items.extend(insert_items)
        return order, insert_cache, runtime_items

    def _project_next_rotation(
        self,
        *,
        now: float,
        alerts: tuple[Any, ...],
        mode: object,
        rotation_count: int,
        read_only: bool,
    ) -> CycleOrderProjection:
        """Capture one pure projection for inspection or mutation."""
        focus = focus_mode_from_inputs(alerts, self._alert_focus_policy, mode)
        projected_timers, due_deferred_keys = deferred_focus_projection(
            self._registry,
            now=now,
            last_pushed_at=self._last_pushed_at,
            focus=focus,
            previously_focused=self._focus_mode_active,
        )
        static_order = tuple(self._registry.static_order(focus=focus))
        now_iso = _rotation_now_iso(now)
        order, insert_cache, runtime_items = self._project_cycle_order(
            now=now,
            focus=focus,
            rotation_count=rotation_count,
            alerts=alerts,
            read_only=read_only,
            static_order=static_order,
            due_deferred_keys=due_deferred_keys,
            now_iso=now_iso,
        )
        alert_order = tuple(item["key"] for item in runtime_items if item.get("kind") == "alert")
        placements = tuple(
            (
                placement,
                tuple(
                    item["key"]
                    for item in runtime_items
                    if item.get("kind") == "scheduled_insert" and item.get("placement") == placement
                ),
            )
            for placement in ("after_time", "after_status", "end_of_rotation")
        )
        return CycleOrderProjection(
            registry=self._registry,
            focus=focus,
            projected_timers=projected_timers,
            due_deferred_keys=tuple(due_deferred_keys),
            static_order=static_order,
            alert_order=alert_order,
            scheduled_insert_placements=placements,
            order=tuple(order),
            runtime_items=tuple(runtime_items),
            insert_cache=insert_cache,
        )

    def _rebuild_cycle_order(self) -> None:
        """
        Recalculate the segment sequence for the upcoming rotation.
        Called once at position 0 (the start of each new cycle pass).

        Order:
          id → time → [priority-sorted active alerts] → focus/normal content
        """
        now = time.time()

        # Fire rotation-complete summary on every rotation after the first
        if self._rotation_count > 0:
            elapsed = now - self._rotation_start_ts
            active_alerts = 0
            try:
                if self._active_alerts_fn:
                    active_alerts = self._active_alerts_fn()
            except Exception:
                pass

            log.info(
                "CycleConductor: rotation #%d complete — %d segments pushed, "
                "elapsed=%.0fs, buffer=%.0fs, active_alerts=%d",
                self._rotation_count,
                self._rotation_seg_count,
                elapsed,
                self.estimated_remaining_s,
                active_alerts,
            )

            # Fire Discord cycle_rebuilt embed for rotation observability
            try:
                if self._discord_fn:
                    self._discord_fn(
                        reason="rotation",
                        mode="continuous",
                        interval=int(elapsed),
                        seq_dur=elapsed,
                        segments=self._rotation_seg_count,
                        active_alerts=active_alerts,
                    )
            except Exception:
                log.debug("CycleConductor: discord_fn failed (non-fatal)", exc_info=True)

        self._rotation_count += 1
        self._rotation_seg_count = 0
        self._rotation_start_ts = now

        try:
            alerts = tuple(self._alert_tracker.get_cycle_alerts())
        except Exception:
            alerts = ()
            log.exception("CycleConductor: could not fetch cycle alerts")
        try:
            mode = self._mode_fn() if self._mode_fn else ""
        except Exception:
            mode = ""
        projection = self._project_next_rotation(
            now=now,
            rotation_count=self._rotation_count,
            alerts=alerts,
            mode=mode,
            read_only=False,
        )
        self._focus_mode_active = projection.focus
        self._last_pushed_at = projection.projected_timers
        self._insert_cache = projection.insert_cache
        order = list(projection.order)
        if projection.focus and projection.due_deferred_keys:
            log.info(
                "CycleConductor: deferred routine segments due in focus mode: %s",
                ",".join(projection.due_deferred_keys),
            )
        previous_order = list(self._last_cycle_order)
        self._cycle_order = order
        self._last_cycle_order = list(order)

        alert_count = len([k for k in order if k.startswith("_alert_")])
        if previous_order:
            previous_set = set(previous_order)
            current_set = set(order)
            added = [k for k in order if k not in previous_set]
            removed = [k for k in previous_order if k not in current_set]
        else:
            added = list(order)
            removed = []
        log.info(
            "CycleConductor: starting rotation #%d — %d segments (%d active alerts, focus=%s)",
            self._rotation_count,
            len(order),
            alert_count,
            projection.focus,
        )
        try:
            if self._discord_fn and previous_order and (added or removed):
                self._discord_fn(
                    reason="order-rebuild",
                    mode="focus" if projection.focus else "normal",
                    interval=0,
                    seq_dur=0.0,
                    segments=len(order),
                    active_alerts=alert_count,
                    added=added,
                    removed=removed,
                    order_preview=order[:10],
                )
        except Exception:
            log.debug("CycleConductor: discord_fn failed during order rebuild", exc_info=True)

    async def _push_next_segment(self) -> bool:
        """
        Advance the position by one and push that segment to Liquidsoap.
        Returns True if audio was actually pushed, False if nothing was ready.
        """
        if self._interrupt_hold:
            return False

        if self._position_in_rotation == 0 or not self._cycle_order:
            self._rebuild_cycle_order()

        key = self._cycle_order[self._position_in_rotation]
        self._position_in_rotation = (self._position_in_rotation + 1) % len(self._cycle_order)

        try:
            if key.startswith("_alert_"):
                dur = self._push_tracker_alert(key[len("_alert_") :])
            elif key.startswith("_insert_"):
                dur = self._push_scheduled_insert(key)
            elif (
                definition := self._registry.get(key)
            ) is not None and definition.builder.kind is SegmentBuilderKind.CONDUCTOR_LIVE_TIME:
                dur = await self._push_live_time()
            else:
                dur = self._push_cached(key)
        except Exception:
            log.exception("CycleConductor: error pushing segment key=%s", key)
            return False

        if dur > 0.0:
            self._total_pushed_s += dur
            return True

        return False

    # ------------------------------------------------------------------
    #  Push helpers
    # ------------------------------------------------------------------

    def _push_scheduled_insert(self, key: str) -> float:
        item = self._insert_cache.get(key) or {}
        insert_id = str(item.get("insert_id") or key[len("_insert_") :])
        audio = Path(str(item.get("audio_path") or ""))
        if not audio.exists():
            log.warning("CycleConductor: scheduled insert audio missing id=%s path=%s", insert_id, audio)
            return 0.0

        title = str(item.get("title") or "Scheduled announcement.")
        duration = float(item.get("duration_seconds") or 0.0)
        if duration <= 0.0:
            try:
                duration = wav_duration_seconds(audio)
            except Exception:
                duration = 0.0

        meta = self._np_meta_fn(
            title=title,
            kind="cycle",
            extra={
                "sw_cycle_key": "insert",
                "sw_insert_id": insert_id,
                "sw_insert_kind": str(((item.get("meta") or {}).get("source_type")) or item.get("kind") or ""),
            },
        )
        try:
            self._telnet.push_cycle(str(audio), meta=meta)
        except Exception as exc:
            log.exception("CycleConductor: telnet push_cycle failed for scheduled insert id=%s", insert_id)
            self._diagnose_liquidsoap(
                FOUNDATION_CODES["liquidsoap.control_failed"],
                "Liquidsoap rejected a scheduled cycle insert publication command.",
                exc,
            )
            return 0.0

        self._rotation_seg_count += 1
        if self._mark_insert_aired_fn:
            try:
                self._mark_insert_aired_fn(insert_id, self._rotation_count)
            except Exception:
                log.exception("CycleConductor: failed to mark scheduled insert aired id=%s", insert_id)
        log.info("CycleConductor: → insert_%s (%.1fs) %s", insert_id, duration, title)
        return max(0.1, duration)

    def _push_cached(self, key: str) -> float:
        """
        Push a SegmentStore-cached segment to the cycle queue.
        Returns the segment duration, or 0.0 if the segment is unavailable
        (placeholder, missing audio).  Missing segments are silently skipped
        so a temporarily unavailable product never stops the broadcast.
        """
        entry = self._store.get(key)
        if not segment_entry_eligible_to_air(entry):
            log.debug("CycleConductor: skipping unavailable segment key=%s", key)
            return 0.0
        if entry is None:
            return 0.0

        audio = Path(entry.audio_path)
        title = self._registry.title_for(key, entry.title or key)
        meta = self._np_meta_fn(
            title=title,
            kind="cycle",
            extra={"sw_cycle_key": key},
        )
        try:
            self._telnet.push_cycle(str(audio), meta=meta)
        except Exception as exc:
            log.exception("CycleConductor: telnet push_cycle failed key=%s", key)
            self._diagnose_liquidsoap(
                FOUNDATION_CODES["liquidsoap.control_failed"],
                f"Liquidsoap rejected cycle segment publication for {key[:64]}.",
                exc,
            )
            return 0.0

        self._rotation_seg_count += 1
        self._last_pushed_at[key] = time.time()
        if self._mark_segment_aired_fn is not None:
            try:
                self._mark_segment_aired_fn(key)
            except Exception:
                log.exception("CycleConductor: failed to persist airing evidence key=%s", key)
        log.info("CycleConductor: → %s (%.1fs)", key, entry.duration_s)
        return entry.duration_s

    def _push_tracker_alert(self, alert_id: str) -> float:
        """
        Push a pre-synthesised AlertTracker voice segment from the store.
        The SegmentRefresher is responsible for keeping these in sync.
        Returns duration or 0.0 if not ready.
        """
        store_key = f"_alert_{alert_id}"
        entry = self._store.get(store_key)

        if not segment_entry_eligible_to_air(entry):
            log.debug(
                "CycleConductor: alert segment not ready id=%s",
                alert_id,
            )
            return 0.0
        if entry is None:
            return 0.0

        audio = Path(entry.audio_path)
        if not audio.exists():
            return 0.0

        try:
            # Best-effort title from the tracker
            ae = self._alert_tracker._alerts.get(alert_id)
            np_title = f"{ae.event}." if ae else entry.title or "Active alert."
            meta = self._np_meta_fn(
                title=np_title,
                kind="cycle",
                extra={"sw_cycle_key": "alert", "sw_alert_id": alert_id},
            )
            self._telnet.push_cycle(str(audio), meta=meta)
        except Exception as exc:
            log.exception(
                "CycleConductor: failed pushing tracker alert id=%s",
                alert_id,
            )
            self._diagnose_liquidsoap(
                FOUNDATION_CODES["liquidsoap.control_failed"],
                "Liquidsoap rejected active-alert cycle segment publication.",
                exc,
            )
            return 0.0

        self._rotation_seg_count += 1
        log.info(
            "CycleConductor: → alert_%s (%.1fs)",
            alert_id,
            entry.duration_s,
        )
        return entry.duration_s

    async def _push_live_time(self) -> float:
        """
        Synthesise the current time *right now* and push to Liquidsoap.

        Because synthesis happens at push time (not at build time), the spoken
        time is always within a few seconds of accurate when it plays — no
        drift from cache age or queue depth.

        Double-buffering (cycle_time_0.wav / cycle_time_1.wav) ensures we
        never overwrite a file Liquidsoap has already queued but not yet played.
        The cycle is ~15-30 minutes; the two buffers are always stale before
        reuse.
        """
        now = dt.datetime.now(tz=self._tz)
        text = f"The current time is, {_fmt_time(now)}, {_short_tz(now)}."

        # Rotate to the next buffer slot
        wav = self._time_bufs[self._time_buf_idx]
        self._time_buf_idx = (self._time_buf_idx + 1) % _TIME_BUF_COUNT

        try:
            with tempfile.TemporaryDirectory(prefix=".cycle-time-", dir=str(self._audio_dir)) as root:
                staging = Path(root)
                raw = staging / "cycle-time-tts.wav"
                gap = staging / "cycle-time-gap.wav"
                complete = staging / "cycle-time-completed.wav"
                await self._synthesizer.synthesize(text, raw, purpose="routine")
                write_silence_wav(gap, self._seg_gap_s, self._sample_rate)
                concat_wavs(complete, [gap, raw, gap])
                dur = wav_duration_seconds(complete)

                if self._interrupt_hold:
                    log.debug("CycleConductor: discarded rendered live time during interrupt hold")
                    return 0.0
                os.replace(complete, wav)

            meta = self._np_meta_fn(
                title=self._registry.title_for("time"),
                kind="cycle",
                extra={"sw_cycle_key": "time"},
            )
            try:
                self._telnet.push_cycle(str(wav), meta=meta)
            except Exception as exc:
                self._diagnose_liquidsoap(
                    FOUNDATION_CODES["liquidsoap.control_failed"],
                    "Liquidsoap rejected live-time cycle segment publication.",
                    exc,
                )
                raise

            self._rotation_seg_count += 1
            log.info("CycleConductor: → time (%.1fs) %r", dur, text)
            return dur

        except Exception:
            log.exception("CycleConductor: failed to synth/push live time segment")
            return 0.0

    def _diagnose_liquidsoap(
        self,
        code: str,
        message: str,
        exception: BaseException | None = None,
    ) -> None:
        sink = self._diagnostic_sink
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            return
        try:
            _ = emit(
                code,
                component="cycle-conductor",
                message=message,
                operational_effect="Routine or interrupt broadcast publication is degraded at the Liquidsoap boundary.",
                recovery_action="Inspect Liquidsoap readiness and reconcile the publication boundary before retrying.",
                exception=exception,
                source_id="cycle-conductor",
            )
        except Exception:
            return
