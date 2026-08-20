from __future__ import annotations

import asyncio
import contextlib
import logging
from importlib import import_module
from typing import Any

from ..lifecycle_records import LifecycleStage
from ..nwws.source import (
    NwwsProductEnvelope,
    NwwsSourceAdmissionFence,
    ProductSink,
    build_nwws_source,
)
from .station_feed_runtime import hydrate_persisted_alerts as _sf_hydrate_persisted_alerts
from .station_feed_runtime import purge_legacy_synthetic_alerts as _sf_purge_legacy_synthetic_alerts


# Optional CAP (api.weather.gov/alerts/active)
def _optional_class(module_name: str, class_name: str) -> type[Any] | None:
    try:
        candidate = getattr(import_module(module_name), class_name)
    except Exception:
        return None
    return candidate if isinstance(candidate, type) else None


NwsCapPoller = _optional_class("seasonalweather.alerts.cap_nws", "NwsCapPoller")
CapAlertEvent = _optional_class("seasonalweather.alerts.cap_nws", "CapAlertEvent")

# Optional IPAWS CAP (apps.fema.gov IPAWS Open feed)
IpawsCapPoller = _optional_class("seasonalweather.alerts.ipaws_cap", "IpawsCapPoller")
IpawsCapEvent = _optional_class("seasonalweather.alerts.ipaws_cap", "IpawsCapEvent")

# Optional ERN/GWES SAME monitor (Level 3 source)
ErnGwesMonitor = _optional_class("seasonalweather.broadcast.ern_gwes", "ErnGwesMonitor")
ErnSameEvent = _optional_class("seasonalweather.broadcast.ern_gwes", "ErnSameEvent")


log = logging.getLogger("seasonalweather")


class _NwwsQueueSink(ProductSink):
    """Controller consumer boundary for normalized NWWS envelopes."""

    def __init__(
        self,
        queue: asyncio.Queue[NwwsProductEnvelope],
        fence: NwwsSourceAdmissionFence,
        source: object,
    ) -> None:
        self._queue = queue
        self._fence = fence
        self._source = source

    def accept(self, envelope: NwwsProductEnvelope) -> bool:
        """Fence and admit synchronously; no await can split the decision."""
        if not self._fence.admits(self._source):
            return False
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull:
            return False
        return True


def _build_controller_owned_nwws_source(owner: Any) -> Any:
    """Compose NWWS without coupling it to the global reload generation."""
    return build_nwws_source(
        owner.jid,
        owner.password,
        owner.nwws_server,
        owner.nwws_port,
        room_jid=owner.cfg.nwws.room,
        nick=owner.cfg.nwws.nick,
        stall_seconds=owner.cfg.nwws.resiliency.stall_seconds,
        muc_confirm_seconds=owner.cfg.nwws.resiliency.muc_confirm_seconds,
        start_wait_seconds=owner.cfg.nwws.resiliency.start_wait_seconds,
        join_wait_seconds=owner.cfg.nwws.resiliency.join_wait_seconds,
        backoff_max_seconds=owner.cfg.nwws.resiliency.backoff_max_seconds,
        generation=0,
        diagnostic_sink=getattr(owner, "nwws_diagnostic_sink", None),
    )


class SeasonalWeatherServiceRuntime:
    """Owns SeasonalWeather task startup and first-exception supervision.

    The orchestrator still owns subsystem construction and source-specific runtimes;
    this class only starts them, registers health probes, and applies the same
    first-failure cancellation semantics previously held by Orchestrator.run().
    """

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    async def run(self) -> None:
        o = self.owner
        work, audio, cache, logs = o._paths()
        for p in (work, audio, cache, logs):
            p.mkdir(parents=True, exist_ok=True)
        if o.lifecycle_records is not None:
            o.lifecycle_records.stage(LifecycleStage.STORAGE_READY, ready=False)

        await o._wait_for_liquidsoap()
        if o.lifecycle_records is not None:
            o.lifecycle_records.stage(LifecycleStage.BROADCAST_PATH_READY, ready=False)
        o._clear_liquidsoap_queues_on_startup()
        o.discord.service_started(
            cap_enabled=o.cfg.cap.enabled,
            ern_enabled=o.cfg.ern.enabled,
            tests_enabled=o.cfg.tests.enabled,
            mode=o.mode,
        )
        # Startup source-state embeds are low-volume and help distinguish
        # intentionally disabled sources from unhealthy ones in Discord ops.
        try:
            _source_health = getattr(o.discord, "source_health", None)
            if _source_health is not None:
                _source_health(
                    source="NWWS-OI",
                    status="disabled" if (o.cfg.nwws.credentials_defaulted or not o.cfg.nwws.enabled) else "enabled",
                    severity="warning" if (o.cfg.nwws.credentials_defaulted or not o.cfg.nwws.enabled) else "ok",
                    details={
                        "allowed_wfos": ",".join(o.cfg.nwws.allowed_wfos) or "all",
                        "toneout_products": len(o.cfg.policy.toneout_product_types),
                    },
                )
                _source_health(
                    source="CAP API",
                    status="enabled" if o.cfg.cap.enabled else "disabled",
                    severity="ok" if o.cfg.cap.enabled else "warning",
                    details={
                        "dryrun": o.cfg.cap.dryrun,
                        "full": o.cfg.cap.full.enabled,
                        "voice": o.cfg.cap.voice.enabled,
                    },
                )
                _source_health(
                    source="IPAWS",
                    status="enabled" if o.cfg.ipaws.enabled else "disabled",
                    severity="ok" if o.cfg.ipaws.enabled else "warning",
                )
                _source_health(
                    source="ERN",
                    status="enabled" if o.cfg.ern.enabled else "disabled",
                    severity="ok" if o.cfg.ern.enabled else "warning",
                )
        except Exception:
            log.debug("Discord source health startup summary failed", exc_info=True)

        # --- Persistent alert state: restore from disk, drop expired ---
        _loaded = 0
        _purged = 0
        try:
            _loaded = o.alert_tracker.load()
            _purged = o.alert_tracker.purge_expired()
            log.info(
                "AlertTracker: loaded %d entries, purged %d expired on startup",
                _loaded, _purged,
            )
        except Exception:
            log.exception("AlertTracker: startup load/purge failed")
        # _TRACKER_DL_
        with contextlib.suppress(Exception):
            o.discord.alerttracker_lifecycle(
                loaded=_loaded,
                purged=_purged,
                active=len(o.alert_tracker.get_cycle_alerts()),
            )

        try:
            _sf_removed_legacy = _sf_purge_legacy_synthetic_alerts()
            if _sf_removed_legacy:
                log.info(
                    "Station feed: removed %d legacy synthetic CAP row(s) on startup",
                    _sf_removed_legacy,
                )
            _sf_hydrated = _sf_hydrate_persisted_alerts()
            if _sf_hydrated:
                log.info(
                    "Station feed: hydrated %d persisted row(s) into runtime cache",
                    _sf_hydrated,
                )
        except Exception:
            log.exception("Station feed: persisted-state startup initialization failed")

        supervisor = o.supervisor

        async def _health_probe_cap_api() -> None:
            await o.api.active_alerts(o.cycle_builder.alert_areas)

        async def _health_probe_nws_api() -> None:
            await o.api.latest_product_id("HWO", "LWX")

        o.health_state.register_probe("cap_api", _health_probe_cap_api)
        o.health_state.register_probe("nws_api", _health_probe_nws_api)

        if o.cfg.nwws.credentials_defaulted or not o.cfg.nwws.enabled:
            o.health_state.mark_disabled("nwws_oi", "nwws_disabled")
        if not o.cfg.cap.enabled:
            o.health_state.mark_disabled("cap_api", "cap_disabled")

        def _health_changed(_ctx) -> None:
            try:
                o.refresher.trigger_immediate("id", "health", "status")
                o._schedule_cycle_refill("health-state-change")
            except Exception:
                log.exception("Health state change refresh failed")

        if o.lifecycle.is_shutting_down:
            return
        if o.lifecycle_records is not None:
            o.lifecycle_records.stage(LifecycleStage.SOURCES_STARTING, ready=False)
        supervisor.create_task(
            o.health_state.run_forever(on_change=_health_changed),
            name="health_state",
            required=False,
        )
        o.alert_audio.start_supervised(supervisor)

        # CycleConductor + SegmentRefresher own routine cycle scheduling.
        supervisor.create_task(
            o.conductor.run(),
            name="conductor",
            required=True,
        )
        supervisor.create_task(
            o.refresher.run(),
            name="segment_refresher",
            required=True,
        )
        supervisor.create_task(
            o.pns_runtime.run_backfill_loop(),
            name="pns_api_backfill",
            required=False,
        )
        supervisor.create_task(
            o.now_runtime.run(),
            name="now_cycle_worker",
            required=False,
        )
        supervisor.create_task(
            o.now_runtime.run_backfill_loop(),
            name="now_api_backfill",
            required=False,
        )

        if o.cfg.nwws.credentials_defaulted:
            log.warning(
                "NWWS-OI disabled because NWWS_JID/NWWS_PASSWORD are unset or still use the example CHANGEME values; "
                "update /etc/seasonalweather/seasonalweather.env to enable NWWS-OI."
            )
        elif not o.cfg.nwws.enabled:
            log.info("NWWS-OI disabled (set nwws.enabled: true in config.yaml to enable)")
        else:
            try:
                source = _build_controller_owned_nwws_source(o)
            except Exception as exc:
                log.warning(
                    "NWWS-OI source adapter could not be constructed (%s); NWWS-OI is disabled.",
                    type(exc).__name__,
                )
            else:
                o.nwws_source = source
                admission_fence = getattr(o, "nwws_admission_fence", None)
                if admission_fence is None:
                    admission_fence = NwwsSourceAdmissionFence()
                    o.nwws_admission_fence = admission_fence
                admission_fence.activate(source)

                async def _stop_nwws() -> None:
                    admission_fence.retire(source)
                    await source.drain()
                    await source.stop()

                supervisor.create_task(
                    source.start(_NwwsQueueSink(o.nwws_queue, admission_fence, source)),
                    name="nwws_xmpp",
                    required=False,
                    stop=_stop_nwws,
                    stop_timeout_seconds=o.lifecycle.timeouts.source_stop_seconds,
                )
                supervisor.create_task(
                    o.nwws_runtime.run(),
                    name="nwws_consumer",
                    required=False,
                )
        # CycleConductor runs the cycle continuously.

        if o.cfg.cap.enabled:
            if NwsCapPoller is None or CapAlertEvent is None:
                log.warning("CAP enabled but cap_nws.py import failed; CAP is disabled.")
            else:
                kwargs: dict[str, Any] = dict(
                    out_queue=o.cap_queue,
                    same_fips_allow=o.cfg.service_area.same_fips_all,
                    poll_seconds=o.cfg.cap.poll_seconds,
                    user_agent=o.cfg.cap.user_agent,
                    ledger_path=o.cfg.cap.ledger_path,
                    ledger_max_age_days=o.cfg.cap.ledger_max_age_days,
                    database=o.database,
                )
                url = o.cfg.cap.url.strip()
                if url:
                    kwargs["url"] = url

                cap = NwsCapPoller(**kwargs)
                supervisor.create_task(
                    cap.run_forever(),
                    name="cap_poller",
                    required=False,
                    stop=cap.aclose,
                    stop_timeout_seconds=o.lifecycle.timeouts.source_stop_seconds,
                )
                supervisor.create_task(
                    o.cap_runtime.run(),
                    name="cap_consumer",
                    required=False,
                )
                log.info("CAP ingest enabled (dryrun=%s full=%s voice=%s)", o.cfg.cap.dryrun, o.cfg.cap.full.enabled, o.cfg.cap.voice.enabled)
        else:
            log.info("CAP ingest disabled (set cap.enabled: true in config.yaml to enable)")

        if o.cfg.ipaws.enabled:
            if IpawsCapPoller is None or IpawsCapEvent is None:
                log.warning("IPAWS enabled but ipaws_cap.py import failed; IPAWS is disabled.")
            else:
                ipaws_poller = IpawsCapPoller(
                    out_queue=o.ipaws_queue,
                    same_fips_allow=o.cfg.service_area.same_fips_all,
                    poll_seconds=o.cfg.ipaws.poll_seconds,
                    user_agent=o.cfg.ipaws.user_agent,
                    url=o.cfg.ipaws.url,
                    ledger_path=o.cfg.ipaws.ledger_path,
                    ledger_max_age_days=o.cfg.ipaws.ledger_max_age_days,
                    database=o.database,
                )
                supervisor.create_task(
                    ipaws_poller.run_forever(),
                    name="ipaws_poller",
                    required=False,
                    stop=ipaws_poller.aclose,
                    stop_timeout_seconds=o.lifecycle.timeouts.source_stop_seconds,
                )
                supervisor.create_task(
                    o.ipaws_runtime.run(),
                    name="ipaws_consumer",
                    required=False,
                )
                log.info(
                    "IPAWS ingest enabled (dryrun=%s full_events=%s)",
                    o.cfg.ipaws.dryrun,
                    ",".join(sorted(set(o.cfg.ipaws.full_events))),
                )
        else:
            log.info("IPAWS ingest disabled (set ipaws.enabled: true in config.yaml to enable)")

        if o.cfg.ern.enabled:
            if ErnGwesMonitor is None or ErnSameEvent is None:
                log.warning("ERN enabled but ern_gwes.py import failed; ERN is disabled.")
            else:
                url = o.cfg.ern.url.strip()
                if not url:
                    log.warning("ERN enabled but SEASONAL_ERN_URL is empty; ERN is disabled.")
                else:
                    ern_cfg = o.cfg.ern
                    mon = ErnGwesMonitor(
                        out_queue=o.ern_queue,
                        same_fips_allow=o.cfg.service_area.same_fips_all,
                        url=url,
                        sample_rate=ern_cfg.sample_rate,
                        dedupe_seconds=ern_cfg.dedupe_seconds,
                        trigger_ratio=ern_cfg.trigger_ratio,
                        tail_seconds=ern_cfg.tail_seconds,
                        confidence_min=ern_cfg.confidence_min,
                        name=ern_cfg.name,
                        decoder_backend=ern_cfg.decoder_backend,
                    )
                    supervisor.create_task(
                        mon.run_forever(),
                        name="ern_monitor",
                        required=False,
                    )
                    supervisor.create_task(
                        o.ern_relay_runtime.run(),
                        name="ern_consumer",
                        required=False,
                    )
                    log.info(
                        "ERN monitor enabled (dryrun=%s url=%s relay=%s decoder=%s)",
                        o.cfg.ern.dryrun,
                        url,
                        o.cfg.ern.relay.enabled,
                        ern_cfg.decoder_backend,
                    )
        else:
            log.info("ERN monitor disabled (set ern.enabled: true in config.yaml to enable)")

        o.tests_runtime.start_scheduler(supervisor=supervisor)

        if o.db_housekeeper is not None:
            supervisor.create_task(
                o.db_housekeeper.run_forever(),
                name="database_housekeeping",
                required=False,
                stop=o.db_housekeeper.stop,
            )

        supervisor.create_task(
            o.discord.start(),
            name="discord_log_drain",
            required=False,
            stop=o.discord.aclose,
        )
        if o.lifecycle.is_shutting_down:
            return
        o.lifecycle.mark_running()
        if o.lifecycle_records is not None:
            o.lifecycle_records.stage(LifecycleStage.SERVICE_READY, ready=True)
        await o.lifecycle.wait_for_shutdown()
