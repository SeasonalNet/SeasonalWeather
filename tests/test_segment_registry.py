from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from seasonalweather.broadcast.segment_registry import (
    DEFAULT_SEGMENT_REGISTRY,
    SegmentBuilderKind,
    SegmentBuilderReference,
    SegmentCapabilityRequirement,
    SegmentFailurePolicy,
    SegmentFocusPolicy,
    SegmentRegistry,
    SegmentRegistryError,
)
from seasonalweather.broadcast.segment_store import SegmentEntry


def _cycle_config(*, spc: bool = True, cwf: bool = True, marine_obs: bool = True, hwo: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        spc=SimpleNamespace(enabled=spc),
        cwf=SimpleNamespace(enabled=cwf),
        marine_obs=SimpleNamespace(enabled=marine_obs),
        hwo=SimpleNamespace(speak_unavailable=hwo),
    )


def test_registry_contains_every_static_segment_exactly_once() -> None:
    keys = tuple(item.key for item in DEFAULT_SEGMENT_REGISTRY.definitions)
    assert keys == (
        "id",
        "time",
        "health",
        "status",
        "hwo",
        "spc",
        "zfp",
        "fcst",
        "cwf",
        "obs",
        "marine_obs",
        "outro",
    )
    assert len(keys) == len(set(keys))


def test_duplicate_key_rejection_is_a_governed_diagnostic() -> None:
    duplicate = replace(DEFAULT_SEGMENT_REGISTRY.get("obs"), title="duplicate")
    with pytest.raises(SegmentRegistryError) as caught:
        SegmentRegistry((DEFAULT_SEGMENT_REGISTRY.get("obs"), duplicate))

    assert caught.value.issue.code == "SWSEG1001"
    assert caught.value.issue.blocking is True
    assert caught.value.issue.path.to_human() == "obs"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("failure_policy", "retain_last_known_good", "failure policy"),
        ("focus_policy", "core", "focus policy"),
        ("builder", replace(DEFAULT_SEGMENT_REGISTRY.get("obs").builder, kind="cached"), "builder"),
        (
            "capability_requirements",
            (SegmentCapabilityRequirement(name=""),),
            "capability",
        ),
    ),
)
def test_malformed_authoritative_definition_is_rejected_before_consumption(
    field: str, value: object, message: str
) -> None:
    invalid = replace(DEFAULT_SEGMENT_REGISTRY.get("obs"), **{field: value})
    with pytest.raises(SegmentRegistryError) as caught:
        SegmentRegistry((invalid,))

    assert caught.value.issue.code == "SWSEG1001"
    assert message in caught.value.issue.message


def test_malformed_failure_policy_cannot_reach_refresher_exception_handling() -> None:
    invalid = replace(DEFAULT_SEGMENT_REGISTRY.get("obs"), failure_policy="retain_last_known_good")
    with pytest.raises(SegmentRegistryError) as caught:
        SegmentRegistry((invalid,))

    assert caught.value.issue.code == "SWSEG1001"
    assert "AttributeError" not in caught.value.issue.message


def test_refresh_beyond_maximum_age_is_rejected_deterministically() -> None:
    invalid = replace(DEFAULT_SEGMENT_REGISTRY.get("obs"), max_age_seconds=899)
    with pytest.raises(SegmentRegistryError) as caught:
        SegmentRegistry((invalid,))

    assert caught.value.issue.code == "SWSEG2001"
    assert "maximum age" in caught.value.issue.message


def test_ambiguous_order_and_missing_order_are_rejected_deterministically() -> None:
    duplicate_order = replace(DEFAULT_SEGMENT_REGISTRY.get("obs"), normal_order=10)
    with pytest.raises(SegmentRegistryError) as duplicate_error:
        SegmentRegistry((DEFAULT_SEGMENT_REGISTRY.get("health"), duplicate_order))
    assert duplicate_error.value.issue.code == "SWSEG2001"
    assert "ordering position" in duplicate_error.value.issue.message

    missing_order = replace(DEFAULT_SEGMENT_REGISTRY.get("obs"), normal_order=None)
    with pytest.raises(SegmentRegistryError) as missing_error:
        SegmentRegistry((missing_order,))
    assert missing_error.value.issue.code == "SWSEG2001"
    assert "missing normal or focus ordering" in missing_error.value.issue.message

    missing_time_order = replace(DEFAULT_SEGMENT_REGISTRY.get("time"), focus_order=None)
    with pytest.raises(SegmentRegistryError) as time_error:
        SegmentRegistry((missing_time_order,))
    assert time_error.value.issue.code == "SWSEG2001"


def test_normal_and_focus_order_are_registry_owned_and_repeatable() -> None:
    first = DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config())
    second = DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config())

    assert first.static_order(focus=False) == (
        "id",
        "time",
        "health",
        "status",
        "hwo",
        "spc",
        "zfp",
        "fcst",
        "cwf",
        "obs",
        "marine_obs",
    )
    assert first.static_order(focus=True) == ("id", "time", "health", "status", "hwo", "spc", "obs")
    assert first.content_keys(focus=False) == (
        "health",
        "status",
        "hwo",
        "spc",
        "zfp",
        "fcst",
        "cwf",
        "obs",
        "marine_obs",
    )
    assert first.content_keys(focus=True) == ("health", "status", "hwo", "spc", "obs")
    assert first.deferred_focus_keys() == ("zfp", "fcst", "marine_obs", "cwf")
    assert first.static_order(focus=False) == second.static_order(focus=False)
    assert first.static_order(focus=True) == second.static_order(focus=True)
    assert first.content_keys(focus=False) == second.content_keys(focus=False)
    assert first.content_keys(focus=True) == second.content_keys(focus=True)


def test_disabled_optional_segments_are_not_selected_by_consumers() -> None:
    resolved = DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config(spc=False, cwf=False, marine_obs=False, hwo=False))

    assert resolved.enabled("spc") is False
    assert resolved.enabled("cwf") is False
    assert resolved.enabled("marine_obs") is False
    assert resolved.enabled("hwo") is True
    assert resolved.fallback_enabled("hwo") is False
    assert "spc" not in resolved.content_keys(focus=False)
    assert "cwf" not in resolved.refresh_keys()
    assert "marine_obs" not in resolved.deferred_focus_keys()


def test_enablement_is_a_mapping_to_typed_configuration_not_yaml() -> None:
    resolved = DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config(spc=False))
    assert resolved.enabled("spc") is False
    assert DEFAULT_SEGMENT_REGISTRY.get("spc").enablement.config_path == ("spc", "enabled")


def test_policy_metadata_and_capability_requirements_are_immutable_and_declarative() -> None:
    definition = DEFAULT_SEGMENT_REGISTRY.get("fcst")
    assert definition.failure_policy is SegmentFailurePolicy.RETAIN_LAST_KNOWN_GOOD
    assert definition.focus_policy is SegmentFocusPolicy.DEFERRED
    assert definition.capability_requirements[0].name == "tts.synthesis.v1"
    assert definition.capability_requirements[0].parameters == {"format": "wav"}
    assert definition.policy_metadata == {}
    with pytest.raises(TypeError):
        definition.policy_metadata["mutated"] = True
    requirement = definition.capability_requirements[0]
    with pytest.raises(TypeError):
        requirement.parameters["format"] = "mp3"
    assert requirement.parameters == {"format": "wav"}
    runtime_requirement = requirement.to_runtime_requirement()
    assert runtime_requirement.parameters == {"format": "wav"}
    runtime_requirement.parameters["format"] = "mp3"
    assert requirement.parameters == {"format": "wav"}

    first = DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config()).get("fcst")
    second = DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config()).get("fcst")
    assert first.capability_requirements[0] is second.capability_requirements[0]
    assert first.capability_requirements[0].parameters == {"format": "wav"}


def test_minimum_air_interval_failure_and_capability_policy_are_registry_owned() -> None:
    resolved = DEFAULT_SEGMENT_REGISTRY.resolve()
    assert resolved.minimum_air_interval("zfp") == 20 * 60
    assert resolved.minimum_air_interval("cwf") == 40 * 60
    assert resolved.minimum_air_interval("health") == 0
    assert all(
        item.failure_policy is SegmentFailurePolicy.RETAIN_LAST_KNOWN_GOOD
        for item in DEFAULT_SEGMENT_REGISTRY.definitions
    )
    requirements = tuple(
        (item.key, tuple(requirement.name for requirement in item.capability_requirements))
        for item in DEFAULT_SEGMENT_REGISTRY.definitions
    )
    assert requirements[-1] == ("outro", ())
    assert all(name == ("tts.synthesis.v1",) for _key, name in requirements[:-1])


def test_registry_state_and_derived_views_are_immutable() -> None:
    resolved = DEFAULT_SEGMENT_REGISTRY.resolve()
    assert isinstance(DEFAULT_SEGMENT_REGISTRY.definitions, tuple)
    assert isinstance(resolved.definitions, tuple)
    with pytest.raises(AttributeError):
        resolved.definitions.append(None)
    with pytest.raises(AttributeError):
        DEFAULT_SEGMENT_REGISTRY.get("obs").title = "mutated"


def test_aliases_and_unknown_keys_have_deterministic_lookup() -> None:
    resolved = DEFAULT_SEGMENT_REGISTRY.resolve()
    assert resolved.get("hwo-unavailable") is resolved.get("hwo")
    assert resolved.is_managed("hwo-unavailable") is True
    assert resolved.get("unknown") is None
    assert resolved.title_for("unknown") == "unknown"


def test_registry_boundary_has_positive_and_negative_architecture_fixtures() -> None:
    root = Path(__file__).resolve().parents[1]
    registry_source = (root / "seasonalweather/broadcast/segment_registry.py").read_text(encoding="utf-8")
    valid_fixture = (
        root / "tests/architecture/fixtures/valid/seasonalweather/broadcast/segment_registry_consumer.py"
    ).read_text(encoding="utf-8")
    invalid_fixture = (
        root / "tests/architecture/fixtures/invalid/seasonalweather/broadcast/segment_registry_authority.py"
    ).read_text(encoding="utf-8")

    assert "yaml" not in registry_source
    assert "DEFAULT_SEGMENT_REGISTRY" in valid_fixture
    assert "_SEGMENT_TITLES" in invalid_fixture


def test_consumers_and_configuration_keep_registry_as_the_only_static_authority() -> None:
    root = Path(__file__).resolve().parents[1]
    consumer_paths = (
        root / "seasonalweather/broadcast/conductor.py",
        root / "seasonalweather/broadcast/segment_store.py",
        root / "seasonalweather/broadcast/segment_refresher.py",
        root / "seasonalweather/main.py",
    )
    forbidden = ("_BASE_CONTENT", "_FOCUS_CONTENT", "_DEFAULT_INTERVALS", "_SEGMENT_TITLES")
    assert all(not any(term in path.read_text(encoding="utf-8") for term in forbidden) for path in consumer_paths)
    conductor_source = (root / "seasonalweather/broadcast/conductor.py").read_text(encoding="utf-8")
    assert 'order: list[str] = ["id", "time"]' not in conductor_source
    registry_source = (root / "seasonalweather/broadcast/segment_registry.py").read_text(encoding="utf-8")
    assert "yaml.safe_load" not in registry_source
    assert "CapabilityRegistry(" not in registry_source


def test_hwo_enablement_and_unavailable_fallback_are_distinct_registry_policies() -> None:
    unavailable = DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config(hwo=False))
    available = DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config(hwo=True))

    assert unavailable.enabled("hwo") is True
    assert unavailable.fallback_enabled("hwo") is False
    assert available.enabled("hwo") is True
    assert available.fallback_enabled("hwo") is True


def test_hwo_fallback_execution_preserves_real_hwo_enablement() -> None:
    from seasonalweather.broadcast.cycle import CycleBuilder

    config = _cycle_config(hwo=False)
    builder = CycleBuilder(
        api=SimpleNamespace(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=None,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(config),
    )
    assert builder._registry.enabled("hwo") is True
    assert builder._hwo_unavailable_segment() is None

    fallback_builder = CycleBuilder(
        api=SimpleNamespace(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=None,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config(hwo=True)),
    )
    unavailable = fallback_builder._hwo_unavailable_segment()
    assert unavailable is not None and unavailable.key == "hwo-unavailable"
    assert fallback_builder._registry.enabled("hwo") is True


def test_hwo_build_path_keeps_real_content_airable_when_fallback_is_disabled() -> None:
    from seasonalweather.broadcast.cycle import CycleBuilder, CycleContext

    class Product:
        product_text = "Hazardous Weather Outlook\nReal HWO text."

    class Api:
        async def latest_product_id(self, *_args):
            return "hwo-id"

        async def get_product(self, _product_id):
            return Product()

    async def no_synopsis(_ctx):
        return None

    async def no_spc(_ctx, _now):
        return None

    async def no_cwf(_ctx):
        return None

    async def no_obs(_ctx):
        return None, None

    async def no_marine(_ctx, _product):
        return None

    builder = CycleBuilder(
        api=Api(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=None,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config(hwo=False)),
    )
    builder._build_synopsis_text = no_synopsis
    builder._build_spc_outlook_text = no_spc
    builder._build_cwf_text = no_cwf
    builder._build_obs_rwr_segment = no_obs
    builder._build_marine_obs_segment = no_marine

    ctx = CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None)
    segments = asyncio.run(builder.build_segments("station", "area", "disclaimer", ctx))
    hwo_segments = [segment for segment in segments if segment.key.startswith("hwo")]

    assert builder._registry.enabled("hwo") is True
    assert builder._registry.content_keys(focus=False).index("hwo") >= 0
    assert [segment.key for segment in hwo_segments] == ["hwo"]
    assert "Real HWO text" in hwo_segments[0].text


def test_cycle_builder_none_config_preserves_legacy_optional_defaults() -> None:
    from seasonalweather.broadcast.cycle import CycleBuilder

    legacy = CycleBuilder(
        api=SimpleNamespace(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=None,
        work_dir="/tmp",
    )
    assert legacy._registry.enabled("spc") is False
    assert legacy._registry.enabled("cwf") is False
    assert legacy._registry.enabled("marine_obs") is False
    assert legacy._registry.enabled("hwo") is True
    assert legacy._registry.fallback_enabled("hwo") is True

    configured = _cycle_config()
    configured.rwr = SimpleNamespace(pressure_cache_hours=3, pressure_trend_threshold_inhg=0.02)
    configured.obs = SimpleNamespace(aliases={})
    current = CycleBuilder(
        api=SimpleNamespace(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=configured,
        work_dir="/tmp",
    )
    assert current._registry.enabled("spc") is True
    assert current._registry.enabled("cwf") is True
    assert current._registry.enabled("marine_obs") is True


def test_builder_references_map_to_real_current_execution_seams() -> None:
    references = {
        item.key: (item.builder.owner, item.builder.operation, item.builder.kind)
        for item in DEFAULT_SEGMENT_REGISTRY.definitions
    }
    assert references["id"] == (
        "seasonalweather.broadcast.segment_refresher",
        "SegmentRefresher._refresh_id",
        SegmentBuilderKind.REFRESHER_ID,
    )
    assert references["status"] == (
        "seasonalweather.broadcast.segment_refresher",
        "SegmentRefresher._refresh_status",
        SegmentBuilderKind.REFRESHER_STATUS,
    )
    assert references["time"] == (
        "seasonalweather.broadcast.conductor",
        "CycleConductor._push_live_time",
        SegmentBuilderKind.CONDUCTOR_LIVE_TIME,
    )
    assert all(
        reference
        == (
            "seasonalweather.broadcast.cycle",
            "CycleBuilder.build_segments",
            SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS,
        )
        for key, reference in references.items()
        if key not in {"id", "status", "time"}
    )


@pytest.mark.parametrize(
    ("owner", "operation"),
    (
        ("", "SegmentRefresher._refresh_id"),
        ("seasonalweather.broadcast.segment_refresher", ""),
        ("seasonalweather.broadcast.fictitious", "SegmentRefresher._refresh_id"),
        ("seasonalweather.broadcast.segment_refresher", "SegmentRefresher._missing"),
    ),
)
def test_fictitious_builder_owner_or_operation_is_rejected_before_dispatch(owner: str, operation: str) -> None:
    source = DEFAULT_SEGMENT_REGISTRY.get("id")
    invalid = replace(source, builder=SegmentBuilderReference(owner, operation, SegmentBuilderKind.REFRESHER_ID))

    with pytest.raises(SegmentRegistryError) as caught:
        SegmentRegistry((invalid,))

    assert caught.value.issue.code == "SWSEG1001"
    assert "builder" in caught.value.issue.message


@pytest.mark.parametrize(
    ("key", "kind"),
    (
        ("obs", SegmentBuilderKind.REFRESHER_ID),
        ("id", SegmentBuilderKind.REFRESHER_STATUS),
        ("status", SegmentBuilderKind.REFRESHER_ID),
        ("obs", SegmentBuilderKind.CONDUCTOR_LIVE_TIME),
        ("fcst", SegmentBuilderKind.CONDUCTOR_LIVE_TIME),
        ("time", SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS),
    ),
)
def test_builder_kind_and_segment_role_must_match_existing_seam(key: str, kind: SegmentBuilderKind) -> None:
    source = DEFAULT_SEGMENT_REGISTRY.get(key)
    owner = source.builder.owner
    operation = source.builder.operation
    if kind is SegmentBuilderKind.REFRESHER_ID:
        owner, operation = (
            "seasonalweather.broadcast.segment_refresher",
            "SegmentRefresher._refresh_id",
        )
    elif kind is SegmentBuilderKind.REFRESHER_STATUS:
        owner, operation = (
            "seasonalweather.broadcast.segment_refresher",
            "SegmentRefresher._refresh_status",
        )
    elif kind is SegmentBuilderKind.CONDUCTOR_LIVE_TIME:
        owner, operation = (
            "seasonalweather.broadcast.conductor",
            "CycleConductor._push_live_time",
        )
    elif kind is SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS:
        owner, operation = "seasonalweather.broadcast.cycle", "CycleBuilder.build_segments"
    invalid = replace(source, builder=SegmentBuilderReference(owner, operation, kind))

    with pytest.raises(SegmentRegistryError) as caught:
        SegmentRegistry((invalid,))

    assert caught.value.issue.code == "SWSEG1001"
    assert "segment role" in caught.value.issue.message


def test_refresher_dispatch_uses_validated_id_seam() -> None:
    from seasonalweather.broadcast.segment_refresher import SegmentRefresher

    resolved = DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config())
    refresher = SegmentRefresher(
        store=SimpleNamespace(),
        cycle_builder=SimpleNamespace(),
        tts=SimpleNamespace(),
        alert_tracker=SimpleNamespace(),
        ctx_fn=lambda: None,
        station_name="station",
        service_area_name="area",
        disclaimer="disclaimer",
        tz=__import__("zoneinfo").ZoneInfo("UTC"),
        sample_rate=8000,
        registry=resolved,
    )
    called: list[bool] = []

    async def fake_refresh_id() -> None:
        called.append(True)

    refresher._refresh_id = fake_refresh_id
    asyncio.run(refresher._refresh_one_untracked("id"))
    assert called == [True]


def test_every_default_refresher_definition_dispatches_to_its_current_seam() -> None:
    from seasonalweather.broadcast.segment_refresher import SegmentRefresher

    refresher = SegmentRefresher(
        store=SimpleNamespace(),
        cycle_builder=SimpleNamespace(),
        tts=SimpleNamespace(),
        alert_tracker=SimpleNamespace(),
        ctx_fn=lambda: None,
        station_name="station",
        service_area_name="area",
        disclaimer="disclaimer",
        tz=__import__("zoneinfo").ZoneInfo("UTC"),
        sample_rate=8000,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config()),
    )
    calls: list[tuple[str, str]] = []

    async def fake_id() -> None:
        calls.append(("id", "refresher"))

    async def fake_status() -> None:
        calls.append(("status", "refresher"))

    async def fake_build(key: str) -> None:
        calls.append((key, "cycle"))

    refresher._refresh_id = fake_id
    refresher._refresh_status = fake_status
    refresher._refresh_via_build = fake_build

    for definition in DEFAULT_SEGMENT_REGISTRY.definitions:
        asyncio.run(refresher._refresh_one_untracked(definition.key))

    assert calls == [
        ("id", "refresher"),
        ("health", "cycle"),
        ("status", "refresher"),
        ("hwo", "cycle"),
        ("spc", "cycle"),
        ("zfp", "cycle"),
        ("fcst", "cycle"),
        ("cwf", "cycle"),
        ("obs", "cycle"),
        ("marine_obs", "cycle"),
        ("outro", "cycle"),
    ]


def test_conductor_static_builder_dispatch_uses_validated_live_time_seam() -> None:
    from seasonalweather.broadcast.conductor import CycleConductor

    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config())
    conductor = object.__new__(CycleConductor)
    conductor._interrupt_hold = False
    conductor._position_in_rotation = 0
    conductor._cycle_order = ["time"]
    conductor._registry = registry
    conductor._total_pushed_s = 0.0
    conductor._rebuild_cycle_order = lambda: None
    called: list[str] = []

    async def fake_live_time() -> float:
        called.append("live")
        return 2.0

    def fake_cached(_key: str) -> float:
        called.append("cached")
        return 3.0

    conductor._push_live_time = fake_live_time
    conductor._push_cached = fake_cached
    assert asyncio.run(conductor._push_next_segment()) is True
    assert called == ["live"]
    assert conductor._total_pushed_s == 2.0


@pytest.mark.parametrize("speak_unavailable", [False, True])
def test_refresher_hwo_unavailable_alias_consumption_matches_fallback_policy(speak_unavailable: bool) -> None:
    from seasonalweather.broadcast.cycle import CycleSegment
    from seasonalweather.broadcast.segment_refresher import SegmentRefresher

    class Store:
        def __init__(self) -> None:
            self.placeholders: list[str] = []

        async def mark_placeholder(self, key: str, *_args, **_kwargs) -> None:
            self.placeholders.append(key)

    class Builder:
        async def build_segments(self, **_kwargs):
            return [
                CycleSegment(
                    key="hwo-unavailable",
                    title="Hazardous weather outlook.",
                    text="The hazardous weather outlook from LWX was unavailable.",
                )
            ]

    store = Store()
    refresher = SegmentRefresher(
        store=store,
        cycle_builder=Builder(),
        tts=SimpleNamespace(),
        alert_tracker=SimpleNamespace(),
        ctx_fn=lambda: SimpleNamespace(mode="normal"),
        station_name="station",
        service_area_name="area",
        disclaimer="disclaimer",
        tz=__import__("zoneinfo").ZoneInfo("UTC"),
        sample_rate=8000,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config(hwo=speak_unavailable)),
    )
    synthesized: list[dict[str, object]] = []

    async def fake_synth(**kwargs) -> None:
        synthesized.append(kwargs)

    refresher._synth = fake_synth
    asyncio.run(refresher._refresh_one_untracked("hwo"))

    if speak_unavailable:
        assert store.placeholders == []
        assert synthesized and synthesized[0]["key"] == "hwo"
    else:
        assert synthesized == []
        assert store.placeholders == ["hwo"]


def test_registry_resolution_fails_closed_for_invalid_typed_config_paths() -> None:
    definition = replace(
        DEFAULT_SEGMENT_REGISTRY.get("spc"),
        enablement=replace(DEFAULT_SEGMENT_REGISTRY.get("spc").enablement, config_path=("missing", "enabled")),
    )
    registry = SegmentRegistry(
        tuple(definition if item.key == "spc" else item for item in DEFAULT_SEGMENT_REGISTRY.definitions)
    )
    with pytest.raises(SegmentRegistryError) as caught:
        registry.resolve(_cycle_config())
    assert caught.value.issue.code == "SWSEG1001"


def test_registry_owns_runtime_policy_fields_used_by_store_and_refresher() -> None:
    for key in ("id", "status", "hwo", "fcst", "obs"):
        definition = DEFAULT_SEGMENT_REGISTRY.get(key)
        assert definition.refresh_cadence_seconds == DEFAULT_SEGMENT_REGISTRY.resolve().refresh_cadence(key)
        assert definition.max_age_seconds == DEFAULT_SEGMENT_REGISTRY.resolve().max_age(key)


def test_refresh_cadence_and_maximum_age_remain_distinct_in_store_state() -> None:
    custom = replace(DEFAULT_SEGMENT_REGISTRY.get("obs"), refresh_cadence_seconds=5, max_age_seconds=20)
    resolved = SegmentRegistry((custom,)).resolve()
    assert resolved.refresh_cadence("obs") == 5
    assert resolved.max_age("obs") == 20

    entry = SegmentEntry(
        key="obs",
        title="Current conditions in our area.",
        text="conditions",
        audio_path="/tmp/obs.wav",
        duration_s=1.0,
        last_updated_ts=time.time() - 10,
        refresh_interval_s=5,
        max_age_s=20,
    )
    assert entry.is_stale() is True
    assert entry.is_expired() is False


def test_refresh_failure_policy_is_read_from_the_registry() -> None:
    from seasonalweather.broadcast.segment_refresher import SegmentRefresher

    class Store:
        def __init__(self) -> None:
            self.placeholders: list[str] = []

        async def mark_placeholder(self, key: str, *_args, **_kwargs) -> None:
            self.placeholders.append(key)

    store = Store()
    refresher = SegmentRefresher(
        store=store,
        cycle_builder=SimpleNamespace(),
        tts=SimpleNamespace(),
        alert_tracker=SimpleNamespace(),
        ctx_fn=lambda: None,
        station_name="station",
        service_area_name="area",
        disclaimer="disclaimer",
        tz=__import__("zoneinfo").ZoneInfo("UTC"),
        sample_rate=8000,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(_cycle_config()),
    )

    async def failed_refresh() -> None:
        raise RuntimeError("synthetic refresh failure")

    refresher._refresh_id = failed_refresh
    asyncio.run(refresher._refresh_one_untracked("id"))
    assert store.placeholders == []


def test_existing_consumers_do_not_redeclare_segment_authority() -> None:
    from seasonalweather.broadcast import conductor, segment_refresher

    assert not hasattr(conductor, "_BASE_CONTENT")
    assert not hasattr(conductor, "_FOCUS_CONTENT")
    assert not hasattr(segment_refresher, "_DEFAULT_INTERVALS")
    assert not hasattr(segment_refresher, "_SEGMENT_TITLES")
