"""
broadcast/segment_refresher.py — SegmentRefresher: background segment refresh engine.

Replaces the monolithic "build everything then push everything" pattern.
Each segment's cadence, enablement, and failure policy come from the
authoritative registry in ``segment_registry.py``.  Product-trigger mappings
below are event routing only; they do not define segment policy.

Alert-tracker segments (_alert_{id}) are synthesised on demand when new
entries appear and pruned when entries expire or are cancelled.

Wiring in main.py (summary)
----------------------------
  In Orchestrator.__init__:
    self.refresher = SegmentRefresher(
        store=self._seg_store,
        cycle_builder=self.cycle_builder,
        tts=self.tts,
        alert_tracker=self.alert_tracker,
        ctx_fn=self._make_cycle_ctx,
        station_name=cfg.station.name,
        service_area_name=cfg.station.service_area_name,
        disclaimer=cfg.station.disclaimer,
        tz=self._tz,
        sample_rate=cfg.audio.sample_rate,
    )

  SeasonalWeatherServiceRuntime registers ``self.refresher.run()`` with the
  controller task supervisor.

  In _consume_nwws / _handle_toneout, after relevant products arrive:
    self.refresher.trigger_immediate("hwo")   # HWO received
    self.refresher.trigger_immediate("zfp")   # RWS/AFD received
    self.refresher.trigger_immediate("obs")   # RWR received
    self.refresher.trigger_immediate("spc")   # SPC product
    self.refresher.trigger_immediate("id")    # mode changed

  Add to Orchestrator:
    def _make_cycle_ctx(self) -> CycleContext:
        return CycleContext(
            mode=self.mode,
            last_heightened_ago=self._heightened_ago_str(),
            last_product_desc=self.last_product_desc,
        )
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import AsyncContextManager
from zoneinfo import ZoneInfo

from ..alerts.active import AlertTracker
from ..diagnostics.bindings import SEGMENT_CODES
from ..jobs.worker_client import SynthesisClient
from .cycle import CycleBuilder, CycleContext, CycleSegment, station_id_text
from .segment_builders import SegmentBuildInput, sanitize_error
from .segment_registry import (
    DEFAULT_SEGMENT_REGISTRY,
    ResolvedSegmentRegistry,
    SegmentBuilderKind,
    SegmentFailurePolicy,
)
from .segment_store import SegmentCommitAmbiguousError, SegmentStore

log = logging.getLogger("seasonalweather.segment_refresher")

# How often the refresher polls for stale segments (regardless of events).
_TICK_S: float = 30.0


class SegmentRefreshCancelled(Exception):
    """A command cancellation won before candidate publication."""


class SegmentRefresher:
    """
    Background engine that keeps SegmentStore content fresh.

    On startup it performs a full cold-start population of all segments.
    Afterwards it runs a tick loop that re-synthesises any segment that
    has passed its refresh interval.  External callers can request an
    immediate out-of-band refresh via ``trigger_immediate(*keys)``.

    Each refresh resolves one registry-declared builder method and synthesizes
    only that target key.
    """

    def __init__(
        self,
        *,
        store: SegmentStore,
        cycle_builder: CycleBuilder,
        tts: SynthesisClient,
        alert_tracker: AlertTracker,
        ctx_fn: Callable[[], CycleContext],
        station_name: str,
        service_area_name: str,
        disclaimer: str,
        organization_name: str = "SeasonalNet",
        service_name: str = "I P Weather Radio Station",
        tz: ZoneInfo,
        sample_rate: int,
        registry: ResolvedSegmentRegistry | None = None,
        seg_gap_s: float = 0.45,
        tick_s: float = _TICK_S,
        on_alert_segments_changed: Callable[[str], None] | None = None,
        on_warmup_complete: Callable[[], None] | None = None,
        activity_context: Callable[[], AsyncContextManager[None]] | None = None,
        diagnostic_sink: object | None = None,
    ) -> None:
        self._store = store
        self._builder = cycle_builder
        self._synthesizer = tts
        self._tts = tts
        self._alert_tracker = alert_tracker
        self._ctx_fn = ctx_fn
        self._station_name = station_name
        self._service_area_name = service_area_name
        self._disclaimer = disclaimer
        self._organization_name = organization_name
        self._service_name = service_name
        self._tz = tz
        self._sample_rate = sample_rate
        self._registry = registry or DEFAULT_SEGMENT_REGISTRY.resolve()
        self._seg_gap_s = seg_gap_s
        self._tick_s = tick_s
        self._on_alert_segments_changed = on_alert_segments_changed
        self._on_warmup_complete = on_warmup_complete
        self._activity_context = activity_context
        self._diagnostic_sink: object | None = diagnostic_sink

        # Immediate-refresh request queue
        self._pending: set[str] = set()
        self._deferred_ambiguities: set[str] = set()
        self._pending_alert_sync: bool = False
        self._wake_event: asyncio.Event = asyncio.Event()

        # Retained as reload-compatible state; independent builders do not use
        # a shared whole-cycle result cache.
        self._seg_cache: list[CycleSegment] | None = None
        self._seg_cache_ts: float = 0.0
        self._seg_cache_mode: str = ""

        # Alert segment tracking
        self._known_alert_ids: set[str] = set()

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def trigger_immediate(self, *keys: str) -> None:
        """
        Request an out-of-band refresh for one or more segment keys.
        Safe to call from any async context.
        """
        for k in keys:
            self._pending.add(k)
        self._wake_event.set()

    def notify_alerts_changed(self) -> None:
        """Wake the loop immediately for AlertTracker audio sync."""
        self._pending_alert_sync = True
        self._wake_event.set()

    async def refresh_one(
        self, key: str, *, commit_guard=None, commit_won=None, commit_aborted=None, commit_identity=None
    ) -> None:
        """Run one registry-admitted target refresh for an application service."""
        await self._refresh_one(
            key,
            commit_guard=commit_guard,
            commit_won=commit_won,
            commit_aborted=commit_aborted,
            commit_identity=commit_identity,
        )

    # ------------------------------------------------------------------
    #  Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        log.info("SegmentRefresher: starting (tick=%.0fs)", self._tick_s)

        # Cold start: populate every segment before the conductor begins
        await self._populate_all()
        if self._on_warmup_complete is not None:
            self._on_warmup_complete()

        while True:
            self._wake_event.clear()
            await self._retry_deferred_ambiguities()

            # Process immediately-triggered keys first
            if self._pending:
                pending = set(self._pending)
                self._pending.clear()
                for key in pending:
                    if key not in self._deferred_ambiguities:
                        await self._refresh_commandless_key(key)

            alert_sync_requested = self._pending_alert_sync
            self._pending_alert_sync = False

            # Regular stale-check pass
            for key in self._registry.refresh_keys():
                if key not in self._deferred_ambiguities and self._store.is_stale(key):
                    await self._refresh_commandless_key(key)

            # Sync alert-tracker voice segments.  This runs every tick, and
            # notify_alerts_changed() wakes it immediately after tracker changes.
            await self._sync_alert_segments(requested=alert_sync_requested)

            # Sleep until next tick or woken by trigger_immediate
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self._tick_s,
                )
            except TimeoutError:
                pass

    # ------------------------------------------------------------------
    #  Cold-start population
    # ------------------------------------------------------------------

    async def _populate_all(self) -> None:
        """Populate only missing or stale content segments at startup."""
        log.info("SegmentRefresher: cold-start population beginning")
        for key in self._registry.refresh_keys():
            existing = getattr(self._store, "get", lambda _key: None)(key)
            if existing is not None and not self._store.is_stale(key):
                continue
            await self._refresh_commandless_key(key)
        log.info("SegmentRefresher: cold-start population complete")

    async def _refresh_commandless_key(self, key: str) -> None:
        """Own one background ambiguity without entering ordinary failure policy."""
        try:
            await self._refresh_one(key)
        except SegmentCommitAmbiguousError:
            await self._handle_commandless_ambiguity(key)

    async def _handle_commandless_ambiguity(self, key: str) -> None:
        self._deferred_ambiguities.add(key)
        reconcile = getattr(self._store, "reconcile_commandless_refresh", None)
        outcome = None
        if callable(reconcile):
            try:
                outcome = await reconcile(key)
            except Exception:
                log.exception("SegmentRefresher: commandless ambiguity reconciliation failed key=%s", key)
                self._diagnose(
                    SEGMENT_CODES["publication_reconciliation"],
                    "Segment publication ambiguity reconciliation failed.",
                )
        self._diagnose(
            SEGMENT_CODES["publication_reconciliation"],
            "Segment publication evidence is ambiguous and refresh remains deferred.",
        )
        outcome_value = getattr(outcome, "value", outcome)
        if outcome_value is None or outcome_value == "still_unresolved":
            log.warning("SegmentRefresher: deferred ambiguous background publication key=%s", key)
        else:
            log.warning(
                "SegmentRefresher: isolated ambiguous background publication key=%s outcome=%s",
                key,
                outcome_value,
            )

    async def _synthesize_commandless_key(self, key: str, **kwargs) -> bool:
        if key in self._deferred_ambiguities:
            return False
        try:
            await self._synth(key=key, **kwargs)
        except SegmentCommitAmbiguousError:
            await self._handle_commandless_ambiguity(key)
            return False
        return True

    async def _retry_deferred_ambiguities(self) -> None:
        """Recheck deferred keys once per loop, then let normal refresh resume."""
        reconcile = getattr(self._store, "reconcile_commandless_refresh", None)
        if not callable(reconcile):
            return
        for key in tuple(self._deferred_ambiguities):
            try:
                outcome = await reconcile(key)
            except Exception:
                log.exception("SegmentRefresher: deferred ambiguity retry failed key=%s", key)
                self._diagnose(
                    SEGMENT_CODES["publication_reconciliation"],
                    "Deferred segment publication reconciliation failed.",
                )
                continue
            if getattr(outcome, "value", outcome) != "still_unresolved":
                self._deferred_ambiguities.discard(key)

    # ------------------------------------------------------------------
    #  Segment refresh dispatch
    # ------------------------------------------------------------------

    async def _refresh_one(
        self, key: str, *, commit_guard=None, commit_won=None, commit_aborted=None, commit_identity=None
    ) -> None:
        """Fetch fresh text and re-synthesise audio for *key*."""
        if self._activity_context is not None:
            async with self._activity_context():
                await self._refresh_one_untracked(
                    key,
                    commit_guard=commit_guard,
                    commit_won=commit_won,
                    commit_aborted=commit_aborted,
                    commit_identity=commit_identity,
                )
            return
        await self._refresh_one_untracked(
            key,
            commit_guard=commit_guard,
            commit_won=commit_won,
            commit_aborted=commit_aborted,
            commit_identity=commit_identity,
        )

    async def _refresh_one_untracked(
        self, key: str, *, commit_guard=None, commit_won=None, commit_aborted=None, commit_identity=None
    ) -> None:
        log.debug("SegmentRefresher: refreshing key=%s", key)
        definition = self._registry.get(key)
        if definition is None:
            log.warning("SegmentRefresher: unrecognised key=%s, skipping", key)
            return
        if not self._registry.enabled(key):
            log.debug("SegmentRefresher: disabled key=%s, skipping", key)
            return
        try:
            await self._dispatch_refresh(
                definition.builder.kind,
                key,
                commit_guard=commit_guard,
                commit_won=commit_won,
                commit_aborted=commit_aborted,
                commit_identity=commit_identity,
            )
        except SegmentRefreshCancelled:
            raise
        except SegmentCommitAmbiguousError:
            # Publication ambiguity is authoritative SegmentStore evidence,
            # not an ordinary builder/source failure.  The application
            # service or startup reconciliation must resolve it first.
            raise
        except Exception as exc:
            self._diagnose(
                SEGMENT_CODES["refresh_failed"],
                f"Segment refresh failed for {key[:64]}; bounded freshness policy is applying.",
                exc,
            )
            if hasattr(self._store, "record_failure"):
                await self._store.record_failure(
                    key,
                    sanitize_error(exc) or "segment refresh failed",
                    title=self._registry.title_for(key, key),
                    refresh_interval_s=self._registry.refresh_cadence(key),
                    max_age_s=self._registry.max_age(key),
                )
            if definition.failure_policy.value == SegmentFailurePolicy.RETAIN_LAST_KNOWN_GOOD.value:
                log.exception("SegmentRefresher: refresh failed key=%s; retaining last known good", key)
            else:
                await self._store.mark_placeholder(
                    key,
                    self._registry.title_for(key, key),
                    self._registry.refresh_cadence(key),
                    max_age_s=self._registry.max_age(key),
                )
                log.exception("SegmentRefresher: refresh failed key=%s; marked placeholder", key)
            self._diagnose(
                SEGMENT_CODES["fallback_used"],
                f"Segment refresh fallback was used for {key[:64]}.",
                exc,
            )

    async def _dispatch_refresh(
        self, kind, key: str, *, commit_guard=None, commit_won=None, commit_aborted=None, commit_identity=None
    ) -> None:
        guarded = commit_guard is not None or commit_won is not None or commit_aborted is not None
        kwargs = {
            "commit_guard": commit_guard,
            "commit_won": commit_won,
            "commit_aborted": commit_aborted,
            "commit_identity": commit_identity,
        }
        if kind is SegmentBuilderKind.REFRESHER_ID:
            await self._refresh_id(**kwargs) if guarded else await self._refresh_id()
        elif kind is SegmentBuilderKind.REFRESHER_STATUS:
            await self._refresh_status(**kwargs) if guarded else await self._refresh_status()
        elif kind is SegmentBuilderKind.INDEPENDENT_SEGMENT:
            await self._refresh_via_build(key, **kwargs) if guarded else await self._refresh_via_build(key)
        else:
            definition = self._registry.get(key)
            log.warning(
                "SegmentRefresher: unsupported builder seam=%s key=%s", definition.builder if definition else key, key
            )

    # ------------------------------------------------------------------
    #  Per-segment builders
    # ------------------------------------------------------------------

    async def _refresh_id(
        self, *, commit_guard=None, commit_won=None, commit_aborted=None, commit_identity=None
    ) -> None:
        """
        Rebuild the station ID segment.  The time sentence is intentionally
        omitted — the conductor synthesises the "time" segment live at push
        time so the spoken time is always accurate.
        """
        ctx = self._ctx_fn()
        text = station_id_text(
            ctx,
            self._station_name,
            self._service_area_name,
            self._disclaimer,
            organization_name=self._organization_name,
            service_name=self._service_name,
        )

        await self._synth(
            key="id",
            text=text,
            title=self._registry.title_for("id"),
            interval=self._registry.refresh_cadence("id"),
            max_age=self._registry.max_age("id"),
            publication_fence=commit_guard,
            publication_committed=commit_won,
            publication_aborted=commit_aborted,
            commit_identity=commit_identity,
        )

    async def _refresh_status(
        self, *, commit_guard=None, commit_won=None, commit_aborted=None, commit_identity=None
    ) -> None:
        """Rebuild station status without making an upstream NWS API request."""
        ctx = self._ctx_fn()
        if getattr(ctx, "health_detached_loop_only", False):
            await self._store.mark_placeholder(
                "status",
                self._registry.title_for("status"),
                self._registry.refresh_cadence("status"),
                max_age_s=self._registry.max_age("status"),
            )
            return

        await self._synth(
            key="status",
            text=self._builder.build_status_text(ctx),
            title=self._registry.title_for("status"),
            interval=self._registry.refresh_cadence("status"),
            max_age=self._registry.max_age("status"),
            publication_fence=commit_guard,
            publication_committed=commit_won,
            publication_aborted=commit_aborted,
            commit_identity=commit_identity,
        )

    async def _refresh_via_build(
        self, key: str, *, commit_guard=None, commit_won=None, commit_aborted=None, commit_identity=None
    ) -> None:
        """
        Refresh one segment through its registry-declared independent method.
        """
        ctx = self._ctx_fn()
        definition = self._registry.get(key)
        if definition is None:
            raise RuntimeError(f"independent builder is unavailable for {key}")
        operation = definition.builder.operation.rsplit(".", 1)[-1]
        method = getattr(self._builder, operation, None)
        if method is None:
            raise RuntimeError(f"independent builder is unavailable for {key}")
        candidate = await method(
            SegmentBuildInput(
                key=key,
                context=ctx,
                station_name=self._station_name,
                service_area_name=self._service_area_name,
                disclaimer=self._disclaimer,
            )
        )

        if candidate is None:
            record_failure = getattr(self._store, "record_failure", None)
            if callable(record_failure):
                await record_failure(
                    key,
                    "independent builder returned no candidate",
                    title=self._registry.title_for(key, key),
                    refresh_interval_s=self._registry.refresh_cadence(key),
                    max_age_s=self._registry.max_age(key),
                )
            else:
                await self._store.mark_placeholder(
                    key,
                    self._registry.title_for(key, key),
                    self._registry.refresh_cadence(key),
                    max_age_s=self._registry.max_age(key),
                    error="independent builder returned no candidate",
                )
            log.debug("SegmentRefresher: key=%s unavailable — recorded failure evidence", key)
            return

        await self._synth(
            key=key,
            text=candidate.text,
            title=self._registry.title_for(key, candidate.title or key),
            interval=self._registry.refresh_cadence(key),
            max_age=self._registry.max_age(key),
            provenance=candidate.provenance,
            publication_fence=commit_guard,
            publication_committed=commit_won,
            publication_aborted=commit_aborted,
            commit_identity=commit_identity,
        )

    # ------------------------------------------------------------------
    #  Alert-tracker segment sync
    # ------------------------------------------------------------------

    async def _sync_alert_segments(self, *, requested: bool = False) -> None:
        """
        Ensure every active AlertTracker voice entry has a synthesised store
        entry (``_alert_{id}``), and mark departed entries as placeholders so
        the conductor skips them on the next rotation.
        """
        changed = False
        try:
            purged = self._alert_tracker.purge_expired()
            if purged:
                changed = True
                log.info("SegmentRefresher: purged %d expired AlertTracker entries", purged)

            active = self._alert_tracker.get_cycle_alerts()
            active_ids: set[str] = {ae.id for ae in active}

            # Synthesise newly-appeared entries
            for ae in active:
                store_key = f"_alert_{ae.id}"
                if not self._store.is_ready(store_key):
                    if ae.script_text.strip():
                        log.info(
                            "SegmentRefresher: synthesising alert segment id=%s event=%s",
                            ae.id,
                            ae.event,
                        )
                        if await self._synthesize_commandless_key(
                            key=store_key,
                            text=ae.script_text,
                            title=f"{ae.event}." if ae.event else "Active alert.",
                            interval=0,  # on-demand only; tracker owns expiry
                        ):
                            changed = True
                    else:
                        await self._store.mark_placeholder(
                            store_key,
                            ae.event or "Active alert.",
                            refresh_interval_s=0,
                        )
                        changed = True

            # Update text if it changed (e.g. CON/EXT updated the script)
            for ae in active:
                store_key = f"_alert_{ae.id}"
                existing = self._store.get(store_key)
                if (
                    existing
                    and not existing.is_placeholder
                    and ae.script_text.strip()
                    and existing.text != ae.script_text
                ):
                    log.info(
                        "SegmentRefresher: alert script changed, re-synthesising id=%s",
                        ae.id,
                    )
                    if await self._synthesize_commandless_key(
                        key=store_key,
                        text=ae.script_text,
                        title=f"{ae.event}." if ae.event else "Active alert.",
                        interval=0,
                    ):
                        changed = True

            # Mark departed entries as placeholders
            departed = self._known_alert_ids - active_ids
            for alert_id in departed:
                store_key = f"_alert_{alert_id}"
                existing = self._store.get(store_key)
                if existing and not existing.is_placeholder:
                    await self._store.mark_placeholder(
                        store_key,
                        existing.title or "Expired alert.",
                        refresh_interval_s=0,
                    )
                    log.info(
                        "SegmentRefresher: alert segment expired/cancelled id=%s",
                        alert_id,
                    )
                    changed = True

            self._known_alert_ids = active_ids

            if changed or requested:
                try:
                    if self._on_alert_segments_changed:
                        self._on_alert_segments_changed("alert-segment-sync")
                except Exception:
                    log.debug("SegmentRefresher: alert-change callback failed", exc_info=True)

        except Exception:
            log.exception("SegmentRefresher: alert segment sync failed")
            self._diagnose(
                SEGMENT_CODES["refresh_failed"],
                "Active-alert segment synchronization failed.",
            )

    def _diagnose(self, code: str, message: str, exception: BaseException | None = None) -> None:
        sink = self._diagnostic_sink
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            return
        try:
            _ = emit(
                code,
                component="segment-refresher",
                message=message,
                operational_effect="One or more broadcast segments may be stale, deferred, or represented by a placeholder.",
                recovery_action="Inspect the segment refresh evidence and allow bounded retry or reconciliation to proceed.",
                exception=exception,
                source_id="segment-refresher",
            )
        except Exception:
            return

    # ------------------------------------------------------------------
    #  Audio synthesis helper
    # ------------------------------------------------------------------

    async def _synth(
        self,
        key: str,
        text: str,
        title: str,
        interval: int,
        max_age: int = 0,
        provenance=None,
        publication_fence=None,
        publication_committed=None,
        publication_aborted=None,
        commit_identity=None,
    ) -> None:
        """
        Synthesise *text* for *key* and update the store.

        Delegates to ``SegmentStore.synth_and_update`` which runs the
        blocking TTS call in a thread executor and then atomically replaces
        the stable WAV file.
        """
        dur = await self._store.synth_and_update(
            self._synthesizer,
            key=key,
            title=title,
            text=text,
            refresh_interval_s=interval,
            max_age_s=max_age,
            sample_rate=self._sample_rate,
            seg_gap_s=self._seg_gap_s,
            provenance=provenance,
            publication_fence=publication_fence,
            publication_committed=publication_committed,
            publication_aborted=publication_aborted,
            command_id=commit_identity,
        )
        log.info(
            "SegmentRefresher: synthesised key=%s dur=%.1fs title=%r",
            key,
            dur,
            title,
        )
