"""Typed replacement-resource preparation and activation ports."""

from __future__ import annotations

import datetime as dt
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from seasonalweather.broadcast.cycle import CycleBuilder
from seasonalweather.broadcast.segment_registry import DEFAULT_SEGMENT_REGISTRY, ResolvedSegmentRegistry
from seasonalweather.broadcast.station_feed_runtime import set_app_config as set_station_feed_config
from seasonalweather.config import AppConfig
from seasonalweather.lifecycle import WorkClass
from seasonalweather.same.locations import normalize_same_allow_set
from seasonalweather.same.targeting import SameTargetResolver
from seasonalweather.tts.admission import (
    ControllerLocalQualificationSource,
    P109TtsQualificationAdapter,
    local_options_from_configuration,
)
from seasonalweather.tts.tts import TTS

from .models import ReloadDiff, ReloadDisposition
from .safe_point import TTS as TTS_ACTIVITY
from .safe_point import ActivityRegistry


class PreparedResources(Protocol):
    expected_generation: int
    target_generation: int
    candidate_identity_sha256: str
    configuration: AppConfig

    @property
    def required_disposition(self) -> ReloadDisposition: ...

    @property
    def diff_sha256(self) -> str: ...

    def validate_ready(self) -> None: ...
    def activate(self, *, safe_point_acquired: bool = False) -> object: ...
    def rollback(self) -> None: ...
    async def retire(self) -> None: ...

    def retirement_descriptor(self) -> dict[str, object]: ...


class ResourcePreparer(Protocol):
    async def prepare(
        self,
        configuration: AppConfig,
        *,
        diff: ReloadDiff,
        expected_generation: int,
        target_generation: int,
        candidate_identity_sha256: str,
    ) -> PreparedResources: ...

    def synchronize_generation(self, generation: int) -> None: ...


@dataclass(frozen=True)
class ResourcePlan:
    """Immutable replacement decisions derived only from the trusted diff."""

    changed_paths: tuple[str, ...]
    required_disposition: ReloadDisposition
    diff_sha256: str
    replace_timezone: bool = False
    replace_tts: bool = False
    replace_cycle_builder: bool = False
    replace_segment_registry: bool = False
    replace_targeting: bool = False
    replace_allowed_wfos: bool = False
    update_dedupe: bool = False


def resource_plan_for_diff(diff: ReloadDiff) -> ResourcePlan:
    paths = tuple(entry.path.to_pointer() for entry in diff.entries)
    segments = tuple(entry.path.segments for entry in diff.entries)

    def under(prefix: tuple[str, ...]) -> bool:
        return any(path[: len(prefix)] == prefix for path in segments)

    timezone = under(("station", "timezone"))
    tts = under(("tts",)) or under(("audio", "sample_rate"))
    cycle_builder = any(under(prefix) for prefix in (("cycle",), ("observations",), ("service_area",))) or timezone
    targeting = under(("service_area",)) or timezone
    return ResourcePlan(
        changed_paths=paths,
        required_disposition=diff.disposition,
        diff_sha256=diff.digest,
        replace_timezone=timezone,
        replace_tts=tts,
        replace_cycle_builder=cycle_builder,
        replace_segment_registry=under(("cycle",)),
        replace_targeting=targeting,
        replace_allowed_wfos=under(("nwws", "allowed_wfos")),
        update_dedupe=under(("dedupe", "ttl_seconds")),
    )


@dataclass
class OrchestratorPreparedResources:
    orch: Any
    plan: ResourcePlan
    configuration: AppConfig
    timezone: ZoneInfo | None
    tts: TTS | None
    tts_capability_source: ControllerLocalQualificationSource | None
    tts_capability_check: P109TtsQualificationAdapter | None
    cycle_builder: CycleBuilder | None
    segment_registry: ResolvedSegmentRegistry | None
    same_allow_set: set[str] | None
    targeting: SameTargetResolver | None
    allowed_wfos: set[str] | None
    expected_generation: int
    target_generation: int
    candidate_identity_sha256: str
    _snapshot: list[tuple[Any, str, object]] = field(default_factory=list, repr=False)
    _activation_started: bool = False
    _activated: bool = False
    _rolled_back: bool = False

    @property
    def required_disposition(self) -> ReloadDisposition:
        return self.plan.required_disposition

    @property
    def diff_sha256(self) -> str:
        return self.plan.diff_sha256

    def validate_ready(self) -> None:
        if self.expected_generation < 0 or self.target_generation < 0 or not self.candidate_identity_sha256:
            raise ValueError("prepared resource fence is malformed")
        if self.configuration is self.orch.cfg:
            raise ValueError("prepared resources must not reuse the active configuration object")
        if self.required_disposition is ReloadDisposition.LIVE and any(
            (
                self.plan.replace_timezone,
                self.plan.replace_tts,
                self.plan.replace_cycle_builder,
                self.plan.replace_segment_registry,
                self.plan.replace_targeting,
                self.plan.replace_allowed_wfos,
            )
        ):
            raise ValueError("live resource plan contains a quiescent replacement")

    def activate(self, *, safe_point_acquired: bool = False) -> object:
        self.validate_ready()
        if self.required_disposition is not ReloadDisposition.LIVE and not safe_point_acquired:
            raise ValueError("quiescent resource replacement requires a held safe point")
        if self._activated:
            return self
        assignments = self._assignments()
        self._snapshot = [(owner, name, getattr(owner, name)) for owner, name, _value in assignments]
        self._activation_started = True
        publication_fence = getattr(self.orch, "tts_publication_fence", None)
        guard = publication_fence.hold() if publication_fence is not None else nullcontext()
        try:
            with guard:
                for owner, name, value in assignments:
                    setattr(owner, name, value)
                if self.required_disposition is not ReloadDisposition.LIVE:
                    set_station_feed_config(self.configuration)
        except BaseException:
            # Preserve the native activation failure. The service owns rollback
            # and must be able to retry every restoration independently.
            raise
        self._activated = True
        return self

    def rollback(self) -> None:
        if self._rolled_back or not self._activation_started:
            return
        failures: list[BaseException] = []
        publication_fence = getattr(self.orch, "tts_publication_fence", None)
        guard = publication_fence.hold() if publication_fence is not None else nullcontext()
        with guard:
            for owner, name, value in reversed(self._snapshot):
                try:
                    setattr(owner, name, value)
                except BaseException as exc:
                    failures.append(exc)
            if self.required_disposition is not ReloadDisposition.LIVE:
                try:
                    set_station_feed_config(self._snapshot_configuration())
                except BaseException as exc:
                    failures.append(exc)
        if failures:
            primary = failures[0]
            for secondary in failures[1:]:
                primary.add_note(f"additional rollback failure: {type(secondary).__name__}")
            raise primary
        self._rolled_back = True

    def _snapshot_configuration(self) -> AppConfig:
        for owner, name, value in self._snapshot:
            if owner is self.orch and name == "cfg":
                if isinstance(value, AppConfig):
                    return value
                break
        raise RuntimeError("prepared resource snapshot has no active configuration")

    async def retire(self) -> None:
        if not self._activated or self._rolled_back:
            return
        old_targeting = next(
            (value for owner, name, value in self._snapshot if owner is self.orch and name == "targeting"),
            None,
        )
        client = getattr(old_targeting, "_zone_client", None)
        if client is not None:
            await client.aclose()
        self._close_retired_tts()

    def _close_retired_tts(self) -> None:
        old_tts = next(
            (value for owner, name, value in self._snapshot if owner is self.orch and name == "tts"),
            None,
        )
        close_tts = getattr(old_tts, "close", None)
        if close_tts is not None:
            close_tts()

    def retirement_descriptor(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "resource_kind": "orchestrator_prepared_resources",
            "resource_scope": "controller_process_local",
            "cleanup": "close_superseded_targeting_client",
            "old_generation": self.expected_generation,
            "proof_on_restart": "superseded_process_resource_gone",
        }

    def _assignments(self) -> tuple[tuple[Any, str, object], ...]:
        cfg = self.configuration
        items: list[tuple[Any, str, object]] = [
            (self.orch, "cfg", cfg),
            (self.orch, "configuration_generation", self.target_generation),
        ]
        items.extend(self._orchestrator_assignments())
        items.extend(self._dependent_assignments())
        return tuple(items)

    def _orchestrator_assignments(self) -> tuple[tuple[Any, str, object], ...]:
        cfg = self.configuration
        items: list[tuple[Any, str, object]] = []
        retained_source = getattr(self.orch, "tts_capability_source", None)
        if not self.plan.replace_tts and retained_source is not None:
            items.append((retained_source, "configuration_generation", self.target_generation))
        if self.plan.update_dedupe:
            items.append((self.orch, "_dedupe_ttl_seconds", cfg.dedupe.ttl_seconds))
        if self.plan.replace_timezone:
            items.extend(((self.orch, "_tz", self.timezone), (self.orch, "local_tz", self.timezone)))
        if self.plan.replace_tts:
            items.append((self.orch, "tts", self.tts))
            if self.tts_capability_source is not None:
                items.extend(
                    (
                        (self.orch, "tts_capability_source", self.tts_capability_source),
                        (self.orch, "tts_capability_check", self.tts_capability_check),
                    )
                )
        if self.plan.replace_cycle_builder:
            items.append((self.orch, "cycle_builder", self.cycle_builder))
        if self.segment_registry is not None:
            items.append((self.orch, "segment_registry", self.segment_registry))
        if self.plan.replace_targeting:
            items.extend(
                (
                    (self.orch, "_same_fips_allow_set", self.same_allow_set),
                    (self.orch, "targeting", self.targeting),
                    (self.orch, "target_resolver", self.targeting),
                )
            )
        if self.plan.replace_allowed_wfos:
            items.append((self.orch, "_nwws_allowed_wfos", self.allowed_wfos))
        return tuple(items)

    def _dependent_assignments(self) -> tuple[tuple[Any, str, object], ...]:
        cfg = self.configuration
        items: list[tuple[Any, str, object]] = []
        cap_text = getattr(self.orch, "cap_text", None)
        if cap_text is not None and self.plan.replace_timezone:
            items.append((cap_text, "_tz", self.timezone))
        originator = getattr(self.orch, "audio_originator", None)
        if originator is not None and self.required_disposition is not ReloadDisposition.LIVE:
            items.append((originator, "cfg", cfg))
            if self.plan.replace_tts:
                items.append((originator, "tts", self.tts))
        items.extend(self._refresher_assignments())
        items.extend(self._conductor_assignments())
        return tuple(items)

    def _refresher_assignments(self) -> tuple[tuple[Any, str, object], ...]:
        cfg = self.configuration
        refresher = getattr(self.orch, "refresher", None)
        if refresher is None:
            return ()
        live_fields = {
            "/station/name": (refresher, "_station_name", cfg.station.name),
            "/station/service_area_name": (
                refresher,
                "_service_area_name",
                cfg.station.service_area_name,
            ),
            "/station/disclaimer": (refresher, "_disclaimer", cfg.station.disclaimer),
            "/audio/sample_rate": (refresher, "_sample_rate", cfg.audio.sample_rate),
        }
        items = [live_fields[path] for path in self.plan.changed_paths if path in live_fields]
        if self.plan.replace_cycle_builder:
            items.extend(
                (
                    (refresher, "_builder", self.cycle_builder),
                    (refresher, "_seg_cache", None),
                    (refresher, "_seg_cache_ts", 0.0),
                    (refresher, "_seg_cache_mode", ""),
                )
            )
        if self.segment_registry is not None:
            items.append((refresher, "_registry", self.segment_registry))
        if self.plan.replace_tts:
            items.append((refresher, "_tts", self.tts))
        if self.plan.replace_timezone:
            items.append((refresher, "_tz", self.timezone))
        return tuple(items)

    def _conductor_assignments(self) -> tuple[tuple[Any, str, object], ...]:
        cfg = self.configuration
        items: list[tuple[Any, str, object]] = []
        conductor = getattr(self.orch, "conductor", None)
        if conductor is not None:
            if self.segment_registry is not None:
                items.append((conductor, "_registry", self.segment_registry))
            if self.plan.replace_tts:
                items.append((conductor, "_tts", self.tts))
            if self.plan.replace_timezone:
                items.append((conductor, "_tz", self.timezone))
            if self.plan.replace_segment_registry:
                items.extend(
                    (
                        (conductor, "_cycle_order", []),
                        (conductor, "_position_in_rotation", 0),
                        (conductor, "_last_cycle_order", []),
                    )
                )
            if any(path == "/audio/sample_rate" for path in self.plan.changed_paths):
                items.append((conductor, "_sample_rate", cfg.audio.sample_rate))
            if any(path.startswith("/cycle/alert_focus/") for path in self.plan.changed_paths):
                items.append((conductor, "_alert_focus_policy", cfg.cycle.alert_focus))
        return tuple(items)


class OrchestratorResourcePreparer:
    def __init__(self, orch: Any, activities: ActivityRegistry) -> None:
        self.orch = orch
        self.activities = activities

    async def prepare(
        self,
        configuration: AppConfig,
        *,
        diff: ReloadDiff,
        expected_generation: int,
        target_generation: int,
        candidate_identity_sha256: str,
    ) -> PreparedResources:
        resource_plan = resource_plan_for_diff(diff)
        segment_registry = (
            DEFAULT_SEGMENT_REGISTRY.resolve(configuration.cycle) if resource_plan.replace_segment_registry else None
        )
        timezone = ZoneInfo(configuration.station.timezone) if resource_plan.replace_timezone else None
        capability_registry = getattr(self.orch, "capability_registry", None)
        tts_capability_source = (
            ControllerLocalQualificationSource(
                capability_registry,
                lambda: dt.datetime.now(dt.UTC),
                configured_options=local_options_from_configuration(configuration),
                configuration_generation=target_generation,
                current_generation=lambda: self.orch.configuration_generation,
                publication_fence=getattr(self.orch, "tts_publication_fence", None),
            )
            if resource_plan.replace_tts and capability_registry is not None
            else None
        )
        tts_capability_check = (
            P109TtsQualificationAdapter(
                capability_registry,
                lambda: dt.datetime.now(dt.UTC),
                local_source=tts_capability_source,
            )
            if tts_capability_source is not None
            else None
        )
        tts = (
            TTS(
                backend=configuration.tts.backend,
                local_engine=configuration.tts.local.engine,
                voice=configuration.tts.voice,
                rate_wpm=configuration.tts.rate_wpm,
                volume=configuration.tts.volume,
                sample_rate=configuration.audio.sample_rate,
                text_overrides=configuration.tts.text_overrides,
                vtp_cfg=configuration.tts.voicetext_paul,
                fallback_backend=configuration.tts.fallback_backend,
                configuration_generation=target_generation,
                generation_provider=lambda: self.orch.configuration_generation,
                current_generation=lambda expected: expected is None or expected == self.orch.configuration_generation,
                admission_check=lambda: self.orch.lifecycle.require(WorkClass.TTS),
                activity_context=lambda: self.activities.activity(TTS_ACTIVITY),
                capability_check=tts_capability_check or getattr(self.orch, "tts_capability_check", None),
                execution_executor=getattr(self.orch, "tts_execution_port", None),
                seasonal_ttsd_config=configuration.tts.seasonal_ttsd,
                openai_compatible_config=configuration.tts.openai_compatible,
                tts_data_base=configuration.paths.operational_state_dir,
            )
            if resource_plan.replace_tts
            else None
        )
        builder_registry = segment_registry or getattr(self.orch, "segment_registry", None)
        if builder_registry is None:
            builder_registry = DEFAULT_SEGMENT_REGISTRY.resolve(configuration.cycle)
        builder = (
            CycleBuilder(
                api=self.orch.api,
                tz_name=configuration.station.timezone,
                obs_stations=configuration.observations.stations,
                reference_points=configuration.cycle.reference_points,
                same_fips_all=configuration.service_area.same_fips_all,
                cycle_cfg=configuration.cycle,
                registry=builder_registry,
                work_dir=configuration.paths.operational_state_dir,
            )
            if resource_plan.replace_cycle_builder
            else None
        )
        allow_set = (
            normalize_same_allow_set(configuration.service_area.same_fips_all)
            if resource_plan.replace_targeting
            else None
        )
        targeting = (
            SameTargetResolver(
                cfg=configuration,
                local_tz=timezone or self.orch.local_tz,
                same_fips_allow_set=allow_set or set(),
            )
            if resource_plan.replace_targeting
            else None
        )
        plan = OrchestratorPreparedResources(
            orch=self.orch,
            plan=resource_plan,
            configuration=configuration,
            timezone=timezone,
            tts=tts,
            tts_capability_source=tts_capability_source,
            tts_capability_check=tts_capability_check,
            cycle_builder=builder,
            segment_registry=segment_registry,
            same_allow_set=allow_set,
            targeting=targeting,
            allowed_wfos=(
                self.orch._norm_wfo_set(configuration.nwws.allowed_wfos) if resource_plan.replace_allowed_wfos else None
            ),
            expected_generation=expected_generation,
            target_generation=target_generation,
            candidate_identity_sha256=candidate_identity_sha256,
        )
        plan.validate_ready()
        return plan

    def synchronize_generation(self, generation: int) -> None:
        if generation < 0:
            raise ValueError("configuration generation cannot be negative")
        publication_fence = getattr(self.orch, "tts_publication_fence", None)
        guard = publication_fence.hold() if publication_fence is not None else nullcontext()
        with guard:
            self.orch.configuration_generation = generation
            source = getattr(self.orch, "tts_capability_source", None)
            if source is not None:
                source.configuration_generation = generation
