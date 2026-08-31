from __future__ import annotations

# =========================================================================================
#      MP"""""`MM                                                       dP              MM'"""'YMM
#      M  mmmmm..M                                                       88              M' .mmm. `M
#      M.      `YM .d8888b. .d8888b. .d8888b. .d8888b. 88d888b. .d8888b. 88              M  MMMMMooM dP    dP 88d888b. 88d888b. .d8888b. 88d888b. .d8888b. dP    dP
#      MMMMMMM.  M 88ooood8 88'  `88 Y8ooooo. 88'  `88 88'  `88 88'  `88 88              M  MMMMMMMM 88    88 88'  `88 88'  `88 88ooood8 88'  `88 88'  `"" 88    88
#      M. .MMM'  M 88.  ... 88.  .88       88 88.  .88 88    88 88.  .88 88              M. `MMM' .M 88.  .88 88       88       88.  ... 88    88 88.  ... 88.  .88
#      Mb.     .dM `88888P' `88888P8 `88888P' `88888P' dP    dP `88888P8 dP              MM.     .dM `88888P' dP       dP       `88888P' dP    dP `88888P' `8888P88
#      MMMMMMMMMMM                                                Seasonal_Currency      MMMMMMMMMMM                                                            .88
#                                                                                                                                                           d8888P.
# =========================================================================================
import argparse
import asyncio
import datetime as dt
import hashlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from .build_metadata import BuildInfo, current_build_info
from .config import AppConfig, load_config
from .configuration_reload.safe_point import (
    ALERT as RELOAD_ALERT_ACTIVITY,
)
from .configuration_reload.safe_point import (
    CONDUCTOR as RELOAD_CONDUCTOR_ACTIVITY,
)
from .configuration_reload.safe_point import (
    SEGMENT_REFRESH as RELOAD_SEGMENT_ACTIVITY,
)
from .configuration_reload.safe_point import (
    TTS as RELOAD_TTS_ACTIVITY,
)
from .configuration_reload.safe_point import (
    ActivityRegistry,
)
from .database.postgresql import PostgresPreflight
from .lifecycle import (
    Lifecycle,
    LifecycleState,
    PublicationFence,
    TaskSupervisor,
    WorkClass,
)
from .lifecycle_records import LifecycleRecordWriter, LifecycleStage
from .logging_config import setup_logging
from .nwws.source import NwwsDiagnosticSink, NwwsProductEnvelope, NwwsSource, NwwsSourceAdmissionFence

# Module-level config reference — set once at startup before Orchestrator is created.
_APP_CFG: AppConfig | None = None
# Active alert tracker (persistent cycle state across restarts)
from .alerts.active import AlertTracker
from .alerts.nws_api import NWSApi
from .alerts.product import ParsedProduct, parse_product_text

# VTEC policy + SAME event code libraries — Orchestrator defers to these.
from .alerts.vtec import (
    VTEC_FIND_RE as _VTEC_FIND_RE,
)
from .alerts.vtec import (
    VTEC_PARSE_RE as _VTEC_PARSE_RE,
)
from .broadcast.alert_audio_jobs import AlertAudioDispatcher
from .broadcast.audio_origination import AudioOriginator
from .broadcast.audio_origination import safe_event_code as _safe_event_code
from .broadcast.cap_policy import best_expiry_from_vtec, cap_vtec_list
from .broadcast.cap_runtime import CapRuntime
from .broadcast.conductor import CycleConductor
from .broadcast.cycle import CycleBuilder, CycleContext
from .broadcast.ern_relay_runtime import ErnRelayRuntime
from .broadcast.formatters import FormatterSubsystem
from .broadcast.ipaws_runtime import IpawsRuntime
from .broadcast.manual_runtime import ManualOriginationRuntime
from .broadcast.now_runtime import NowRuntime
from .broadcast.nwws_runtime import NwwsRuntime
from .broadcast.pns_runtime import PnsRuntime
from .broadcast.segment_refresher import SegmentRefresher
from .broadcast.segment_registry import DEFAULT_SEGMENT_REGISTRY
from .broadcast.segment_store import SegmentStore
from .broadcast.service_runtime import SeasonalWeatherServiceRuntime
from .broadcast.tests_runtime import RequiredTestRuntime
from .database.bootstrap import bootstrap_database_from_config
from .database.housekeeping import DatabaseHousekeeper
from .database.inserts import CycleInsertRepository
from .database.station_feed import StationFeedRepository
from .capabilities.registry import CapabilityRegistry
from .capabilities.service import declared_capability_names
from .discord_log import DiscordLogger
from .health_state import HealthStateMachine
from .liquidsoap_telnet import LiquidsoapTelnet
from .same.locations import normalize_same_allow_set as _normalize_same_allow_set
from .same.targeting import SameTargetResolver
from .tts.audio import wav_duration_seconds
from .jobs.worker_client import SynthesisClient, WorkerSynthesisClient
from .tts.remote_client import RemoteSynthesisClient

if TYPE_CHECKING:
    from .alerts.cap_nws import CapAlertEvent
    from .alerts.ipaws_cap import IpawsCapEvent
    from .broadcast.ern_gwes import ErnSameEvent
    from .observability.metrics import MetricsRegistry

log = logging.getLogger("seasonalweather")

from .broadcast.station_feed_runtime import (
    remove_ern_relays_matching as _sf_remove_ern_relays_matching,
)
from .broadcast.station_feed_runtime import (
    set_app_config as _sf_set_app_config,
)
from .broadcast.station_feed_runtime import (
    set_repository as _sf_set_repository,
)
from .broadcast.station_feed_runtime import (
    station_feed_housekeeping_start as _sf_station_feed_hk_start,
)

# _VTEC_FIND_RE and _VTEC_PARSE_RE are now imported from .alerts.vtec above.


def _setup_logging(
    cfg: AppConfig | None = None,
    *,
    role: str | None = None,
    instance_id: str | None = None,
    build_info: BuildInfo | None = None,
    metrics: MetricsRegistry | None = None,
) -> None:
    setup_logging(cfg, role=role, instance_id=instance_id, build_info=build_info, metrics=metrics)


# _env_* helpers removed — all configuration now flows through AppConfig.
# Credentials are accessed via cfg.secrets.* (set once in load_config()).


class Orchestrator:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        lifecycle: Lifecycle | None = None,
        supervisor: TaskSupervisor | None = None,
        capability_registry: CapabilityRegistry | None = None,
        lifecycle_records: LifecycleRecordWriter | None = None,
    ) -> None:
        global _APP_CFG
        _APP_CFG = cfg
        _sf_set_app_config(cfg)
        self.cfg = cfg
        self.lifecycle = lifecycle or Lifecycle(cfg.lifecycle)
        self.supervisor = supervisor or TaskSupervisor(self.lifecycle)
        self.lifecycle_records = lifecycle_records
        self.publication_fence = PublicationFence(self.lifecycle)
        self.reload_activities = ActivityRegistry()
        self.artifact_service: object | None = None
        self.artifact_results: object | None = None
        self.worker_job_service: object | None = None
        self.worker_repository: object | None = None
        self.worker_active_root: Path | None = None
        self.configuration_generation = 0
        self.capability_registry = capability_registry or CapabilityRegistry(
            allowed_capabilities=declared_capability_names()
        )
        self.api = NWSApi()
        self.telnet = LiquidsoapTelnet(
            host=cfg.network.liquidsoap.host,
            port=cfg.network.liquidsoap.port,
            timeout=cfg.network.liquidsoap.timeout_seconds,
        )

        self._tz = ZoneInfo(cfg.station.timezone)
        self.local_tz = self._tz
        self.formatters = FormatterSubsystem(
            local_tz=self._tz,
            cap_vtec_list=cap_vtec_list,
            vtec_tracks=self._vtec_tracks,
            best_expiry_from_vtec=best_expiry_from_vtec,
        )

        # Isolated policy/state machines. main.py only wires inputs/outputs;
        # classification and health decisions live in their own modules.
        self.health_state = HealthStateMachine(cfg.health)

        # NWWS-OI
        self.jid = cfg.secrets.nwws_jid
        self.password = cfg.secrets.nwws_password
        self.nwws_server = cfg.nwws.server
        self.nwws_port = cfg.nwws.port

        # Local synthesis is admitted and committed through a dedicated
        # worker. External provider overlays retain their controller-owned
        # provider client, but can never fall back to local execution here.
        if cfg.tts.backend == "local":
            self.synthesizer: SynthesisClient = WorkerSynthesisClient(
                configuration=cfg,
                input_root=Path(cfg.paths.artifact_dir) / "worker-artifacts" / "staging" / "controller-inputs",
            )
        else:
            self.synthesizer = RemoteSynthesisClient(
                configuration=cfg,
                admission_check=lambda: self.lifecycle.require(WorkClass.TTS),
                activity_context=lambda: self.reload_activities.activity(RELOAD_TTS_ACTIVITY),
            )
        # Compatibility name for extracted components; this object is a
        # synthesis port, never a controller-local engine or handler.
        self.tts = self.synthesizer

        self.mode = "normal"
        self.heightened_until: dt.datetime | None = None
        self.last_heightened_at: dt.datetime | None = None
        self.last_product_desc: str | None = None

        # RWT/RMT gating timestamps
        self.last_toneout_at: dt.datetime | None = None
        self.cap_last_severe_at: dt.datetime | None = None
        self.ern_last_tone_at: dt.datetime | None = None

        # Bootstrap controller-owned SQLite before any component constructs a
        # restart-surviving cache.  Durable state must not silently fall back
        # to files when the database is enabled.
        self.database = bootstrap_database_from_config(cfg) if getattr(cfg.database, "enabled", True) else None
        self.postgresql_preflight = PostgresPreflight(
            configuration=cfg.database.postgres,
            network=cfg.network.postgresql,
        )

        self.segment_registry = DEFAULT_SEGMENT_REGISTRY.resolve(cfg.cycle)
        self.cycle_builder = CycleBuilder(
            api=self.api,
            tz_name=cfg.station.timezone,
            obs_stations=cfg.observations.stations,
            reference_points=cfg.cycle.reference_points,
            same_fips_all=cfg.service_area.same_fips_all,
            cycle_cfg=cfg.cycle,
            registry=self.segment_registry,
            work_dir=cfg.paths.operational_state_dir,
            database=self.database,
        )

        # Fast membership checks for "in-area" targeting
        self._same_fips_allow_set = _normalize_same_allow_set(cfg.service_area.same_fips_all)
        self.targeting = SameTargetResolver(
            cfg=cfg,
            local_tz=self._tz,
            same_fips_allow_set=self._same_fips_allow_set,
        )
        # Public runtime alias retained for extracted source runtimes.
        self.target_resolver = self.targeting

        # --- NWWS flood-gate controls ---
        self._nwws_logger = logging.getLogger("seasonalweather.nwws")
        self._nwws_raw_seen = 0
        self._nwws_rx_log_first_n = cfg.nwws.resiliency.rx_log_first_n
        self._nwws_decision_log_first_n = cfg.nwws.resiliency.decision_log_first_n
        self._nwws_decision_log_every = cfg.nwws.resiliency.decision_log_every
        self._nwws_allowed_wfos = self._norm_wfo_set(getattr(cfg.nwws, "allowed_wfos", []))

        self.nwws_queue: asyncio.Queue[NwwsProductEnvelope] = asyncio.Queue(maxsize=200)
        self.nwws_source: NwwsSource | None = None
        self.nwws_diagnostic_sink: NwwsDiagnosticSink | None = None
        self.cap_diagnostic_sink: object | None = None
        self.ern_diagnostic_sink: object | None = None
        self.database_diagnostic_sink: object | None = None
        self.liquidsoap_diagnostic_sink: object | None = None
        self.nwws_admission_fence = NwwsSourceAdmissionFence()

        # CAP queue (only used if CAP enabled and import succeeded)
        self.cap_queue: asyncio.Queue[CapAlertEvent] = asyncio.Queue(maxsize=200)
        self._cap_voice_last_by_key: dict[tuple[str, str], dt.datetime] = {}
        self._cap_full_last_by_key: dict[tuple[str, str], dt.datetime] = {}

        # IPAWS queue (only used if IPAWS enabled and import succeeded)
        self.ipaws_queue: asyncio.Queue[IpawsCapEvent] = asyncio.Queue(maxsize=200)

        # ERN queue (only used if ERN enabled and import succeeded)
        self.ern_queue: asyncio.Queue[ErnSameEvent] = asyncio.Queue(maxsize=200)

        # ERN relay cooldown (on-air)
        self._ern_relay_last_any_at: dt.datetime | None = None

        # Prevent concurrent cycle flush/push from overlapping.
        self._cycle_lock = asyncio.Lock()

        # CycleConductor handles continuous buffering and live time synthesis.

        # --- Cross-source dedupe (NWWS vs CAP) ---
        self._dedupe_ttl_seconds = cfg.dedupe.ttl_seconds
        self._dedupe_lock = asyncio.Lock()
        self._recent_air_keys: dict[str, dt.datetime] = {}

        # --- NWWS decision visibility counters ---
        self._nwws_seen = 0
        self._nwws_acted = 0

        # --- Embedded SQLite runtime state ---
        self.cycle_insert_repo = CycleInsertRepository(self.database) if self.database is not None else None
        self.db_housekeeper = DatabaseHousekeeper(cfg, self.database) if self.database is not None else None
        self.station_feed_repo = StationFeedRepository(self.database) if self.database is not None else None
        _sf_set_repository(self.station_feed_repo)

        # Start StationFeed housekeeping after SQLite is ready so the public
        # /v1/handled-alerts read model stays tidy.
        _sf_station_feed_hk_start()

        # --- Persistent active alert tracker ---
        # Survives restarts: active watches/warnings are re-queued as cycle segments.
        _tracker_path = Path(cfg.paths.operational_state_dir) / "alert_state.json"
        self.alert_tracker = AlertTracker(_tracker_path, database=self.database)

        # Discord webhook logger (fire-and-forget; starts its drain task in run())
        self.discord = DiscordLogger.from_config(cfg.logs.discord)

        # AudioOriginator owns TTS/WAV/SAME assembly. The orchestrator only
        # decides what alert/message should be aired.
        self.audio_originator = AudioOriginator(
            cfg=cfg,
            synthesizer=self.synthesizer,
            local_tz=self._tz,
            paths=self._paths,
            discord=self.discord,
        )
        self.alert_audio = AlertAudioDispatcher(
            admission_check=lambda: self.lifecycle.require(WorkClass.ALERT),
            publication_fence=self.publication_fence,
            activity_context=lambda: self.reload_activities.async_activity(RELOAD_ALERT_ACTIVITY),
        )
        self.ern_relay_runtime = ErnRelayRuntime(self, self.formatters)
        self.ipaws_runtime = IpawsRuntime(self, self.formatters)
        self.cap_runtime = CapRuntime(self, self.formatters)
        self.pns_runtime = PnsRuntime(self)
        self.now_runtime = NowRuntime(self)
        self.nwws_runtime = NwwsRuntime(self, self.formatters)
        self.tests_runtime = RequiredTestRuntime(self)
        self.manual_runtime = ManualOriginationRuntime(self)
        self.service_runtime = SeasonalWeatherServiceRuntime(self)

        # SegmentStore: persistent per-segment audio cache.
        # Placed last so alert_tracker and all other attributes are available.
        self._seg_store = SegmentStore(
            work_dir=Path(cfg.paths.operational_state_dir),
            audio_dir=Path(cfg.paths.audio_dir),
            database=self.database,
            static_key_predicate=self.segment_registry.is_managed,
        )
        self._seg_store.load()

        # SegmentRefresher: keeps each segment's text + audio up to date
        # independently on its own cadence.
        self.refresher = SegmentRefresher(
            store=self._seg_store,
            cycle_builder=self.cycle_builder,
            tts=self.synthesizer,
            alert_tracker=self.alert_tracker,
            ctx_fn=self._make_cycle_ctx,
            station_name=cfg.station.name,
            service_area_name=cfg.station.service_area_name,
            disclaimer=cfg.station.disclaimer,
            organization_name=cfg.station.organization_name,
            service_name=cfg.station.service_name,
            tz=self._tz,
            sample_rate=cfg.audio.sample_rate,
            registry=self.segment_registry,
            on_alert_segments_changed=lambda reason: self.conductor.notify_flush(reset_rotation=True, reason=reason),
            on_warmup_complete=(
                lambda: (
                    lifecycle_records.stage(LifecycleStage.BACKGROUND_WARMUP_COMPLETE, ready=self.lifecycle.ready)
                    if lifecycle_records is not None
                    else None
                )
            ),
            activity_context=lambda: self.reload_activities.async_activity(RELOAD_SEGMENT_ACTIVITY),
        )

        # CycleConductor: continuous cycle driver.  Flush notifications restart
        # the active-alert priority rotation after alert-state changes.
        self.conductor = CycleConductor(
            store=self._seg_store,
            telnet=self.telnet,
            tts=self.synthesizer,
            alert_tracker=self.alert_tracker,
            tz=self._tz,
            audio_dir=Path(cfg.paths.audio_dir),
            sample_rate=cfg.audio.sample_rate,
            registry=self.segment_registry,
            np_meta_fn=self._np_meta,
            discord_fn=self.discord.cycle_rebuilt,
            active_alerts_fn=lambda: len(self.alert_tracker.get_cycle_alerts()),
            mode_fn=lambda: self.mode,
            alert_focus_policy=cfg.cycle.alert_focus,
            scheduled_inserts_fn=self._cycle_due_inserts,
            scheduled_inserts_snapshot_fn=self._cycle_due_inserts_snapshot,
            mark_insert_aired_fn=self._mark_cycle_insert_aired,
            mark_segment_aired_fn=self._mark_segment_aired,
            activity_context=lambda: self.reload_activities.async_activity(RELOAD_CONDUCTOR_ACTIVITY),
        )
        self.alert_tracker.set_change_callback(self._on_alert_tracker_changed)

    def _utc_iso(self, value: dt.datetime | None = None) -> str:
        when = value or dt.datetime.now(dt.UTC)
        return when.astimezone(dt.UTC).replace(microsecond=0).isoformat()

    def _cycle_due_inserts(
        self, placement: str, rotation_count: int, focus: bool, now_iso: str | None = None
    ) -> list[dict]:
        repo = getattr(self, "cycle_insert_repo", None)
        if repo is None:
            return []
        return cast(
            list[dict],
            repo.list_due(
                placement=placement,
                rotation_count=rotation_count,
                now_iso=now_iso or self._utc_iso(),
                active_alert_focus=focus,
            ),
        )

    def _cycle_due_inserts_snapshot(
        self, placement: str, rotation_count: int, focus: bool, now_iso: str | None = None
    ) -> list[dict]:
        repo = getattr(self, "cycle_insert_repo", None)
        if repo is None:
            return []
        return cast(
            list[dict],
            repo.list_due(
                placement=placement,
                rotation_count=rotation_count,
                now_iso=now_iso or self._utc_iso(),
                active_alert_focus=focus,
                expire=False,
            ),
        )

    def _mark_cycle_insert_aired(self, insert_id: str, rotation_count: int) -> None:
        repo = getattr(self, "cycle_insert_repo", None)
        if repo is None:
            return
        repo.mark_aired(
            insert_id=insert_id,
            aired_at=self._utc_iso(),
            rotation_count=rotation_count,
        )

    def _mark_segment_aired(self, key: str) -> None:
        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        next_eligible = now + dt.timedelta(seconds=self.segment_registry.minimum_air_interval(key))
        self._seg_store.mark_aired(key, now.isoformat(), next_eligible.isoformat())

    def _on_alert_tracker_changed(self, reason: str) -> None:
        """Wake audio synthesis and reset rotation after active-alert state changes."""
        try:
            self.refresher.notify_alerts_changed()
            self.refresher.trigger_immediate("status")
        except Exception:
            log.debug("AlertTracker change: refresher notify failed", exc_info=True)
        try:
            self.conductor.notify_flush(reset_rotation=True, reason=f"alert-state:{reason}")
        except Exception:
            log.debug("AlertTracker change: conductor notify failed", exc_info=True)

    # --- Now Playing / IP-RDS helpers (edit phrases freely) ---

    _NP_ALERT_TEMPLATES = {
        "nwws_full": "{event}.",
        "nwws_update": "Update for a {event}.",
        "nwws_end": "A {event} has ended.",
        "cap_full": "{event}.",
        "cap_update": "Update for a {event}.",
        "ern": "{event} relay.",
        "rwt": "Required weekly test.",
        "rmt": "Required monthly test.",
        "default": "A weather alert has been issued.",
    }

    def _np_meta(self, *, title: str, kind: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        # What players display:
        #   - title/artist/album/song
        # Plus internal keying fields prefixed with sw_ (most players ignore them).
        station = self.cfg.station.name
        artist = self.cfg.station.now_playing_artist or self.cfg.station.organization_name
        album = self.cfg.station.now_playing_album or f"Weather information for {self.cfg.station.service_area_name}"
        t = (title or "").strip() or "SeasonalWeather"
        song = f"{station} — {t}"

        m: dict[str, str] = {
            "title": t,
            "artist": artist,
            "album": album,
            "song": song,
            "sw_station": station,
            "sw_kind": (kind or "").strip(),
        }
        if extra:
            for k, v in extra.items():
                s = str(v).strip()
                if s:
                    m[str(k)] = s
        return m

    def _np_alert_title(self, template_key: str, *, event: str) -> str:
        tpl = self._NP_ALERT_TEMPLATES.get(template_key, self._NP_ALERT_TEMPLATES["default"])
        return tpl.format(event=(event or "Alert").strip())

    def _norm_wfo_set(self, wfos: list[str] | set[str] | tuple[str, ...]) -> set[str]:
        """
        Normalizes allowed WFOs so YAML can use LWX or KLWX interchangeably.
        Also supports 4-letter Kxxx or 3-letter xxx.
        """
        out: set[str] = set()
        for w in wfos or []:
            s = str(w).strip().upper()
            if not s:
                continue
            out.add(s)
            if len(s) == 3:
                out.add("K" + s)
            if len(s) == 4 and s.startswith("K"):
                out.add(s[1:])
        return out

    def _paths(self) -> tuple[Path, Path, Path, Path]:
        work = Path(self.cfg.paths.operational_state_dir)
        audio = Path(self.cfg.paths.audio_dir)
        cache = Path(self.cfg.paths.cache_dir)
        logs = Path(self.cfg.paths.log_dir)
        return work, audio, cache, logs

    async def _wait_for_liquidsoap(self) -> None:
        for _ in range(60):
            if self.telnet.ping():
                log.info("Liquidsoap telnet is reachable")
                return
            await asyncio.sleep(1)
        raise RuntimeError("Liquidsoap telnet did not become reachable (is seasonalweather-liquidsoap running?)")

    def _make_cycle_ctx(self) -> CycleContext:
        """Return a CycleContext reflecting current station state.
        Used by SegmentRefresher so it can build segments without holding
        a reference to the full Orchestrator.
        """
        health = self.health_state.context()
        return CycleContext(
            mode=self.mode,
            last_heightened_ago=self._heightened_ago_str(),
            last_product_desc=self.last_product_desc,
            health_mode=health.mode,
            health_notice=health.notice,
            health_status_line=health.status_line,
            health_detached_loop_only=health.detached_loop_only,
            active_alerts=tuple(self.alert_tracker.get_cycle_alerts()),
        )

    def _update_mode(self) -> None:
        _prev_mode = getattr(self, "mode", "normal")
        now = dt.datetime.now(tz=self._tz)
        if self.heightened_until and now < self.heightened_until:
            self.mode = "heightened"
        else:
            self.mode = "normal"
        if self.mode != _prev_mode:
            try:
                self.discord.mode_changed(old_mode=_prev_mode, new_mode=self.mode)
            except Exception:
                pass
            # Trigger immediate id segment refresh so heightened/normal
            # station ID goes on air on the next cycle rotation.
            try:
                self.refresher.trigger_immediate("id")
            except AttributeError:
                pass  # refresher not yet initialised

    def _heightened_ago_str(self) -> str | None:
        if not self.last_heightened_at:
            return None
        delta = dt.datetime.now(tz=self._tz) - self.last_heightened_at
        mins = int(delta.total_seconds() // 60)
        if mins < 1:
            return "less than one minute"
        if mins < 60:
            return f"{mins} minutes"
        hrs = mins // 60
        rem = mins % 60
        return f"{hrs} hours" if rem == 0 else f"{hrs} hours and {rem} minutes"

    def _schedule_cycle_refill(self, reason: str) -> None:
        """Reset continuous-cycle buffer/order after an external state change."""
        if not self.lifecycle.allows(WorkClass.ROUTINE):
            return
        try:
            self.refresher.trigger_immediate("id", "status")
            self.refresher.notify_alerts_changed()
        except AttributeError:
            pass  # refresher not yet initialised (startup edge case)
        except Exception:
            log.debug("Cycle refill: refresher notify failed", exc_info=True)
        try:
            self.conductor.notify_flush(reset_rotation=True, reason=reason)
        except AttributeError:
            pass  # conductor not yet initialised (startup edge case)

    def _clear_liquidsoap_queues_on_startup(self) -> None:
        """Do not clear Liquidsoap request queues with telnet skip/flush commands.

        On Liquidsoap 2.3.x, both request_queue.flush_and_skip and source.skip()
        can leave a pending skip on an empty request source.  The next pushed
        request is then accepted and prepared, but falls off after the first
        audio frame and the fallback graph returns to the next source/blank.
        This poisoned the first FULL/VOICE alert after every orchestrator
        restart.

        A Liquidsoap service restart is the safe way to clear stale request
        state.  The Python orchestrator must not issue destructive queue
        controls automatically at startup.
        """
        log.info("Liquidsoap startup queue reset skipped; restart Liquidsoap to clear stale request queues")

    def _diagnose_liquidsoap(self, code: str, message: str, exception: BaseException | None = None) -> None:
        sink = getattr(self, "liquidsoap_diagnostic_sink", None)
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            return
        emit(
            code,
            component="liquidsoap-publication",
            message=message,
            operational_effect="Liquidsoap publication or control is degraded at the broadcast boundary.",
            recovery_action="Inspect Liquidsoap readiness and reconcile the publication boundary before retrying.",
            exception=exception,
            source_id="liquidsoap",
        )

    # ---- dedupe helpers ----
    def _sha1_12(self, s: str) -> str:
        h = hashlib.sha1((s or "").encode("utf-8", errors="ignore"), usedforsecurity=False).hexdigest()
        return h[:12]

    def _dedupe_func_full_key(self, event_code: str, same_locs: list[str] | None) -> str | None:
        """
        Cross-source "functional" FULL-alert dedupe key.

        ERN cannot map to VTEC, so we dedupe FULL tone-outs by:
          (event code) + (in-area SAME locations)

        Locations are normalized to:
          - service-area filtered
          - unique
          - order-independent (sorted)

        Returns None if no usable locations exist (avoid deduping on empty targets).
        """
        code_u = _safe_event_code(event_code).strip().upper()
        locs_in = [str(x).strip() for x in (same_locs or []) if str(x).strip()]
        locs = self.target_resolver._filter_same_locations_to_service_area(locs_in)
        if not locs:
            return None
        locs_norm = sorted(set(locs))
        blob = code_u + "|" + ",".join(locs_norm)
        return f"FUNC_FULL:{code_u}:{self._sha1_12(blob)}"

    async def _push_interrupt_audio(self, wav_path, *, meta: dict[str, str] | None = None, full: bool = False) -> None:
        """Push rendered interrupt audio to the proper Liquidsoap priority plane.

        FULL alerts preempt both the cycle plane and lower-priority voice updates.
        VOICE alerts interrupt only the cycle plane and never clear queued/current
        FULL alerts.  Do not cut the cycle plane before admission: cycle must stay
        available as the fallback if Liquidsoap rejects the interrupt request.
        After a successful push, only the guarded cycle reset may run.
        """

        from .diagnostics.bindings import FOUNDATION_CODES

        def _notify_refill_after_failed_push() -> None:
            # If an interrupt push fails, wake the conductor so cycle generation
            # continues promptly.  Cycle is not cut for alert admission, but this
            # keeps fallback material fresh after unexpected Liquidsoap errors.
            try:
                self.conductor.notify_flush(reset_rotation=False, reason="interrupt-push-failed")
            except AttributeError:
                pass
            except Exception:
                log.debug("Cycle refill notification after failed interrupt push failed", exc_info=True)

        async with self._cycle_lock, self.publication_fence.enter():
            mode = "full" if full else "voice"
            if full:
                # Do not pre-flush or pre-skip the FULL/VOICE request queues here.
                # On the Liquidsoap 2.3.x build seen in production, queue control
                # discovery maps flush to flush_and_skip and exposes skip controls.
                # Issuing those commands immediately before push can leave a
                # pending skip on the source and make the replacement alert fall
                # off after a tiny burst.  Keep cycle alive and rely on fallback
                # priority (FULL > VOICE > cycle) for the actual interrupt.
                try:
                    if hasattr(self.telnet, "push_full_alert"):
                        self.telnet.push_full_alert(str(wav_path), meta=meta)
                    else:
                        self.telnet.push_alert(str(wav_path), meta=meta)
                except Exception as exc:
                    _notify_refill_after_failed_push()
                    self._diagnose_liquidsoap(
                        FOUNDATION_CODES["liquidsoap.control_failed"],
                        "Liquidsoap rejected a full-alert publication command.",
                        exc,
                    )
                    raise
            else:
                # Same rule for VOICE updates: avoid same-plane skip/flush before
                # push.  Stale queued VOICE material is less dangerous than
                # dropping the live update after SAME/first audio frames.
                try:
                    if hasattr(self.telnet, "push_voice_alert"):
                        self.telnet.push_voice_alert(str(wav_path), meta=meta)
                    else:
                        self.telnet.push_alert(str(wav_path), meta=meta)
                except Exception as exc:
                    _notify_refill_after_failed_push()
                    self._diagnose_liquidsoap(
                        FOUNDATION_CODES["liquidsoap.control_failed"],
                        "Liquidsoap rejected a voice-alert publication command.",
                        exc,
                    )
                    raise

            # The interrupt is now admitted and available to the priority
            # fallback.  Clear only the paused routine cycle source using the
            # guarded repository alias; never touch FULL/VOICE queue controls.
            # Then hold the conductor so it cannot rebuild a stale backlog while
            # Liquidsoap has the cycle source paused.
            try:
                duration_s = wav_duration_seconds(Path(wav_path))
            except Exception:
                log.exception("Could not determine %s interrupt duration; using conservative hold", mode)
                duration_s = 60.0

            reset_ok = False
            try:
                reset_ok = bool(self.telnet.reset_cycle_safely())
            except AttributeError:
                reset_ok = False
            except Exception as exc:
                log.exception("Safe cycle reset failed after %s interrupt admission", mode)
                self._diagnose_liquidsoap(
                    FOUNDATION_CODES["liquidsoap.publication_reconciliation"],
                    "Liquidsoap cycle reset did not prove the post-publication state.",
                    exc,
                )

            if reset_ok:
                try:
                    self.conductor.notify_interrupt_started(
                        duration_s=duration_s,
                        reason=f"{mode}-interrupt",
                    )
                except AttributeError:
                    pass
                except Exception:
                    # The alert is already admitted and the cycle reset already
                    # happened.  Do not convert a conductor bookkeeping failure
                    # into an alert push failure or trigger duplicate origination.
                    log.exception("Could not start cycle interrupt hold after %s admission", mode)
            else:
                # Preserve alert delivery on mixed-version deployments, but make
                # the stale-cycle risk explicit until radio.liq is updated and
                # Liquidsoap restarted.
                log.error(
                    "Liquidsoap does not expose sw.cycle.reset; cycle freshness protection is disabled for this interrupt"
                )

    async def _render_and_push_interrupt_audio(
        self,
        *,
        source: str,
        full: bool,
        render,
        meta: dict[str, str] | None = None,
    ):
        """Render alert audio through the priority dispatcher, then push it."""

        async def _push(path):
            await self._push_interrupt_audio(path, meta=meta, full=full)

        started = time.monotonic()
        mode = "full" if full else "voice"
        try:
            if full:
                out_path = await self.alert_audio.render_and_push_full(
                    source=source,
                    render=render,
                    push=_push,
                )
            else:
                out_path = await self.alert_audio.render_and_push_voice(
                    source=source,
                    render=render,
                    push=_push,
                )
            try:
                duration_s = wav_duration_seconds(Path(out_path))
            except Exception:
                duration_s = None
            try:
                _audio_log = getattr(self.discord, "audio_pipeline", None)
                if _audio_log is not None:
                    _audio_log(
                        source=source,
                        status="rendered+pushed",
                        mode=mode,
                        path=str(out_path),
                        duration_s=duration_s,
                        backend="tts+same" if full else "tts",
                        cache="miss",
                    )
            except Exception:
                log.debug("Discord audio pipeline audit failed", exc_info=True)
            return out_path
        except Exception as exc:
            try:
                _audio_log = getattr(self.discord, "audio_pipeline", None)
                if _audio_log is not None:
                    _audio_log(
                        source=source,
                        status="failed",
                        mode=mode,
                        duration_s=time.monotonic() - started,
                        fallback=type(exc).__name__,
                    )
            except Exception:
                log.debug("Discord audio pipeline failure audit failed", exc_info=True)
            raise

    def _nwws_api_product_matches_raw(self, parsed: ParsedProduct, api_text: str) -> bool:
        """
        Accept an api.weather.gov product override only when it appears to be the
        same issuance as the NWWS payload we already received.

        This protects against the API returning an older "latest" product for the
        same product type/location, which can otherwise cause stale VTEC, stale
        expiries, and incorrect cross-source dedupe decisions.
        """
        if not api_text or not api_text.strip():
            return False

        api_parsed = parse_product_text(api_text)
        if api_parsed:
            raw_awips = (parsed.awips_id or "").strip().upper()
            api_awips = (api_parsed.awips_id or "").strip().upper()
            if raw_awips and api_awips and raw_awips != api_awips:
                return False

            raw_wfo = (parsed.wfo or "").strip().upper()
            api_wfo = (api_parsed.wfo or "").strip().upper()
            if raw_wfo and api_wfo and raw_wfo != api_wfo:
                return False

        raw_vtec = self._extract_vtec(parsed.raw_text or "")
        api_vtec = self._extract_vtec(api_text)
        raw_track_actions = set(self._vtec_tracks(raw_vtec))
        api_track_actions = set(self._vtec_tracks(api_vtec))
        raw_tracks = {track for (track, _act) in raw_track_actions}
        api_tracks = {track for (track, _act) in api_track_actions}

        # If NWWS already gave us a concrete VTEC action for a concrete track,
        # the API override must agree on at least one identical (track, action)
        # pair. Matching only the track is too weak: a stale EXA/CON product can
        # otherwise override a newer EXP/CAN product on the same ETN and cause a
        # full tone-out for what should have been a voice-only expiration.
        if raw_track_actions:
            return bool(api_track_actions) and bool(raw_track_actions & api_track_actions)

        # If the raw payload has track IDs but no usable action pair match, reject.
        if raw_tracks:
            return bool(api_tracks) and bool(raw_tracks & api_tracks)

        # No VTEC in raw payload: fall back to AWIPS/WFO agreement only.
        return True

    def _extract_vtec(self, text: str) -> list[str]:
        if not text:
            return []
        found = _VTEC_FIND_RE.findall(text)
        out: list[str] = []
        seen: set[str] = set()
        for v in found:
            if v in seen:
                continue
            seen.add(v)
            out.append(v)
            if len(out) >= 6:
                break
        return out

    def _vtec_tracks(self, vtec_list: list[str]) -> list[tuple[str, str]]:
        """
        Return [(track_id, action)] where track_id := OFFICE.PHEN.SIG.ETN, action := NEW/CON/EXT/UPG/etc
        """
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw in vtec_list or []:
            s = "".join(str(raw).split()).strip()
            if not s:
                continue
            m = _VTEC_PARSE_RE.search(s)
            if not m:
                continue
            office = m.group("office")
            phen = m.group("phen")
            sig = m.group("sig")
            etn = m.group("etn")
            act = m.group("act")
            track = f"{office}.{phen}.{sig}.{etn}"
            k = f"{track}|{act}"
            if k in seen:
                continue
            seen.add(k)
            out.append((track, act))
        return out[:12]

    async def _dedupe_prune(self) -> None:
        now = dt.datetime.now(tz=self._tz)
        ttl = float(self._dedupe_ttl_seconds)
        dead: list[str] = []
        for k, ts in self._recent_air_keys.items():
            if (now - ts).total_seconds() > ttl:
                dead.append(k)
        for k in dead:
            self._recent_air_keys.pop(k, None)

    async def _dedupe_reserve(self, keys: list[str]) -> tuple[bool, str]:
        """
        Reserve dedupe keys *before* airing to avoid races.
        If we later fail to air, caller should release.
        """
        now = dt.datetime.now(tz=self._tz)
        async with self._dedupe_lock:
            await self._dedupe_prune()
            for k in keys:
                if k in self._recent_air_keys:
                    return (False, k)
            for k in keys:
                self._recent_air_keys[k] = now
            return (True, "")

    async def _dedupe_release(self, keys: list[str]) -> None:
        async with self._dedupe_lock:
            for k in keys:
                self._recent_air_keys.pop(k, None)

    def _alert_expires_from_ern(self, ev) -> str:
        """Best-effort expiry ISO for an ERN SAME relay from JJJHHMM + TTTT."""
        now_utc = dt.datetime.now(dt.UTC)
        start_utc = self.formatters.ern_start_utc(getattr(ev, "jjjhhmm", None), now_utc=now_utc)
        duration_min = self.formatters.ern_duration_minutes(getattr(ev, "tttt", None))
        if start_utc is not None and duration_min is not None:
            end_utc = start_utc + dt.timedelta(minutes=duration_min)
            if end_utc > now_utc:
                return end_utc.isoformat()
        if duration_min is not None and duration_min > 0:
            return (now_utc + dt.timedelta(minutes=duration_min)).isoformat()
        return (now_utc + dt.timedelta(hours=1)).isoformat()

    def _remove_shadowed_ern_state(self, *, code: str | None, same_locs, reason: str) -> int:
        """Remove ERN relay copies once authoritative CAP/NWWS/IPAWS state covers them."""
        removed = 0
        try:
            removed += self.alert_tracker.remove_matching_source(
                source="ERN",
                code=code,
                same_locs=list(same_locs or []),
                reason=reason,
            )
        except Exception:
            log.exception("AlertTracker: failed removing ERN relay state reason=%s", reason)
        try:
            removed += _sf_remove_ern_relays_matching(code, same_locs)
        except Exception:
            log.exception("Station feed: failed removing ERN relay state reason=%s", reason)
        return removed

    def _remove_matching_ipaws_state(self, *, code: str | None, same_locs, reason: str) -> int:
        """Remove IPAWS active-cycle entries when a CAP/IPAWS cancel kills them."""
        try:
            return self.alert_tracker.remove_matching_source(
                source="IPAWS",
                code=code,
                same_locs=list(same_locs or []),
                reason=reason,
            )
        except Exception:
            log.exception("AlertTracker: failed removing IPAWS state reason=%s", reason)
            return 0

    async def run(self) -> None:
        """Run the SeasonalWeather service runtime."""
        await self.service_runtime.run()
        if self.lifecycle.state is LifecycleState.FAILED:
            raise await self.supervisor.wait_for_fatal()


def _version_command(argv: list[str]) -> int:
    version_parser = argparse.ArgumentParser(prog="seasonalweather version")
    version_parser.add_argument("--json", action="store_true", dest="machine_readable")
    args = version_parser.parse_args(argv)
    info = current_build_info()
    if args.machine_readable:
        print(json.dumps(info.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    else:
        print(info.build_identity)
    return 0


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if effective_argv and effective_argv[0] == "version":
        return _version_command(effective_argv[1:])
    if effective_argv and effective_argv[0] == "auth":
        from .cli.auth import main as auth_main

        return auth_main(effective_argv[1:])
    if effective_argv and effective_argv[0] == "config":
        from .cli.config import main as config_main

        return config_main(effective_argv[1:])
    if effective_argv and effective_argv[0] == "diagnostics":
        from .diagnostics.cli import main as diagnostics_main

        return diagnostics_main(effective_argv[1:])
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/seasonalweather/config.yaml")
    args = ap.parse_args(effective_argv)

    cfg = load_config(args.config)
    _setup_logging(cfg)
    orch = Orchestrator(cfg)
    asyncio.run(orch.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# _DISCORD_LOG_ALL_HOOKS_APPLIED_
