from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

from seasonalweather.alerts.nws_api import NWSProduct
from seasonalweather.broadcast.cycle import CycleBuilder, CycleContext
from seasonalweather.broadcast.offnt2 import parse_offnt2_product, render_offnt2
from seasonalweather.broadcast.segment_builders import SegmentBuildInput
from seasonalweather.broadcast.segment_registry import DEFAULT_SEGMENT_REGISTRY
from seasonalweather.tts.preprocess import clean_for_tts
from seasonalweather.tts.voicetext_paul_vtml import apply_voicetext_paul_vtml

VALID_OFFNT2 = """\
FZNT22 KWBC 181800
OFFNT2

Offshore Waters Forecast
National Weather Service Ocean Prediction Center Washington DC

Synopsis...A cold front crosses the waters tonight.

ANZ450-451-190600-
Coastal waters from Sandy Hook to Manasquan Inlet NJ out 20 nm-
Coastal waters from Manasquan Inlet to Cape Henlopen DE out 20 nm-
Today...Southwest winds 10 to 15 knots.
GALE WARNING IN EFFECT FROM THIS EVENING THROUGH FRIDAY MORNING.
Tonight...Winds becoming west 20 to 25 knots.

ANZ452-453-190600-
Coastal waters from Cape Henlopen to Fenwick Island DE out 20 nm-
Coastal waters from Fenwick Island DE to Chincoteague VA out 20 nm-
Today...Southwest winds 5 to 10 knots.
Tonight...Winds becoming west 10 to 15 knots.

$$
"""

REAL_NWS_LAYOUT_OFFNT2 = """\
000
FZNT22 KWBC 182024
OFFNT2

Offshore Waters Forecast
NWS Ocean Prediction Center Washington DC
424 PM EDT Tue Aug 18 2026

ANZ899-190830-
424 PM EDT Tue Aug 18 2026

.SYNOPSIS FOR MID ATLC WATERS...A cold front will move slowly SE
across waters early tonight, then stall over the N portion later
tonight into Fri night.

$$

ANZ820-190830-
Hudson Canyon to Baltimore Canyon to 1000 FM-
424 PM EDT Tue Aug 18 2026

.TONIGHT...E winds less than 5 kt, becoming variable. Seas 3 to
4 ft. Chance of tstms.

$$

ANZ915-190830-
Between 1000FM and 38.5 N west of 69 W-
424 PM EDT Tue Aug 18 2026

.TONIGHT...N to NE winds 5 to 10 kt. Seas 3 to 5 ft.

$$
"""


def test_parse_offnt2_accepts_expected_identity_and_expands_zone_groups() -> None:
    product = parse_offnt2_product(VALID_OFFNT2)

    assert product is not None
    assert product.awips_id == "OFFNT2"
    assert product.wmo_heading == "FZNT22 KWBC"
    assert product.synopsis == "A cold front crosses the waters tonight"
    assert [zone.zone_ids for zone in product.zones] == [("ANZ450", "ANZ451"), ("ANZ452", "ANZ453")]
    assert product.zones[0].warning_headlines == ("GALE WARNING IN EFFECT FROM THIS EVENING THROUGH FRIDAY MORNING",)


def test_parse_offnt2_rejects_unexpected_region_even_when_product_type_matches() -> None:
    assert parse_offnt2_product(VALID_OFFNT2.replace("FZNT22 KWBC", "FZNT21 KWBC").replace("OFFNT2", "OFFNT1")) is None
    assert parse_offnt2_product(VALID_OFFNT2.replace("FZNT22 KWBC", "FZNT22 KPHI")) is None


def test_real_nws_layout_extracts_anz899_synopsis_and_stops_at_budget() -> None:
    product = parse_offnt2_product(REAL_NWS_LAYOUT_OFFNT2)

    assert product is not None
    assert (
        product.synopsis
        == "A cold front will move slowly SE across waters early tonight, then stall over the N portion later tonight into Fri night"
    )
    assert [zone.zone_ids for zone in product.zones] == [("ANZ820",), ("ANZ915",)]
    assert "424 PM EDT" not in product.zones[0].text
    assert "Hudson Canyon" not in product.zones[0].text

    rendered = render_offnt2(
        product,
        configured_zones=(("ANZ820", "ANZ820"), ("ANZ915", "ANZ915")),
        include_synopsis=True,
        rotate_period_s=1800,
        rotate_step=1,
        now=dt.datetime.fromtimestamp(0, tz=dt.UTC),
        max_chars=500,
        max_airtime_seconds=0,
    )

    assert rendered is not None
    assert "SYNOPSIS FOR" not in rendered
    assert "424 PM EDT" not in rendered
    assert rendered.count("The forecast for") == 2
    assert "The forecast for ANZ915. Tonight." in rendered


def test_real_nws_layout_formats_for_tts_and_voicetext_vtml() -> None:
    product = parse_offnt2_product(REAL_NWS_LAYOUT_OFFNT2)

    assert product is not None
    rendered = render_offnt2(
        product,
        configured_zones=(("ANZ820", "ANZ820"), ("ANZ915", "ANZ915")),
        include_synopsis=True,
        rotate_period_s=1800,
        rotate_step=1,
        now=dt.datetime.fromtimestamp(0, tz=dt.UTC),
        max_chars=1200,
        max_airtime_seconds=90,
    )

    assert rendered is not None
    spoken = clean_for_tts(rendered)
    vtml = apply_voicetext_paul_vtml(spoken)

    assert "Tonight." in spoken
    assert "TONIGHT..." not in spoken
    assert "424 PM EDT" not in spoken
    assert 'alias="feet"' in vtml
    assert 'alias="knots"' in vtml
    assert 'ph="TH AH1 N D ER0 S T OW0 R M Z"' in vtml
    assert 'ph="W IH1 N D Z"' in vtml


def test_render_offnt2_rotates_zones_and_deduplicates_cwf_synopsis() -> None:
    product = parse_offnt2_product(VALID_OFFNT2)
    assert product is not None

    rendered = render_offnt2(
        product,
        configured_zones=(
            ("ANZ450", "New Jersey waters"),
            ("ANZ452", "Delaware and Virginia waters"),
        ),
        include_synopsis=True,
        rotate_period_s=60,
        rotate_step=1,
        now=dt.datetime.fromtimestamp(60, tz=dt.UTC),
        max_chars=1200,
        max_airtime_seconds=90,
        cwf_synopsis=product.synopsis,
    )

    assert rendered is not None
    assert "Synopsis." not in rendered
    assert "Delaware and Virginia waters" in rendered
    assert "New Jersey waters" in rendered
    assert len(rendered) <= 1200


def test_render_offnt2_heightened_mode_preserves_warning_and_defers_routine_material() -> None:
    product = parse_offnt2_product(VALID_OFFNT2)
    assert product is not None

    rendered = render_offnt2(
        product,
        configured_zones=(("ANZ450", "New Jersey waters"), ("ANZ452", "Delaware waters")),
        include_synopsis=True,
        rotate_period_s=1800,
        rotate_step=1,
        now=dt.datetime.fromtimestamp(0, tz=dt.UTC),
        max_chars=800,
        max_airtime_seconds=30,
        heightened=True,
        defer_in_heightened=True,
    )

    assert rendered is not None
    assert "GALE WARNING IN EFFECT" in rendered
    assert "A cold front crosses" not in rendered

    no_warning = parse_offnt2_product(
        VALID_OFFNT2.replace("GALE WARNING IN EFFECT FROM THIS EVENING THROUGH FRIDAY MORNING.\n", "")
    )
    assert no_warning is not None
    assert (
        render_offnt2(
            no_warning,
            configured_zones=(("ANZ450", "New Jersey waters"),),
            include_synopsis=True,
            rotate_period_s=1800,
            rotate_step=1,
            now=dt.datetime.fromtimestamp(0, tz=dt.UTC),
            heightened=True,
            defer_in_heightened=True,
        )
        is None
    )


def test_cycle_builder_offnt2_uses_validated_product_and_provenance(tmp_path) -> None:
    class Api:
        async def offshore_forecast_product(self, _office: str) -> NWSProduct:
            return NWSProduct(
                product_id="OFF-1",
                product_text=VALID_OFFNT2,
                issuance_time="2026-08-18T18:00:00Z",
                product_type="OFF",
                wfo="KWBC",
            )

    config = SimpleNamespace(
        offnt2=SimpleNamespace(
            enabled=True,
            source_office="KWBC",
            product_type="OFF",
            zones=[("ANZ450", "New Jersey waters")],
            include_synopsis=True,
            max_chars_normal=1200,
            max_chars_heightened=800,
            max_airtime_seconds=90,
            rotate_period_s=1800,
            rotate_step=1,
            defer_in_heightened=True,
        ),
        rwr=SimpleNamespace(pressure_cache_hours=3, pressure_trend_threshold_inhg=0.02),
        obs=SimpleNamespace(aliases={}),
        spc=SimpleNamespace(enabled=False),
        cwf=SimpleNamespace(enabled=False),
        marine_obs=SimpleNamespace(enabled=False),
        hwo=SimpleNamespace(speak_unavailable=True),
    )
    builder = CycleBuilder(
        api=Api(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=config,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(config),
        work_dir=str(tmp_path),
    )

    candidate = asyncio.run(
        builder.build_offnt2_segment(
            SegmentBuildInput(
                key="offnt2",
                context=CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
                station_name="station",
                service_area_name="area",
                disclaimer="disclaimer",
            )
        )
    )

    assert candidate is not None
    assert candidate.key == "offnt2"
    assert candidate.provenance.product_identifier == "OFF-1"
    assert candidate.provenance.issuing_office == "KWBC"


def test_cycle_composer_deduplicates_offnt2_synopsis_after_cwf(tmp_path, monkeypatch) -> None:
    cwf_text = """\
Coastal Waters Forecast
National Weather Service

.SYNOPSIS...A cold front crosses the waters tonight.

ANZ450-451-190600-
Coastal waters from Sandy Hook to Manasquan Inlet NJ out 20 nm-
Today...Southwest winds 10 to 15 knots.
"""

    class Api:
        async def coastal_waters_forecast_product(self, _office: str) -> NWSProduct:
            return NWSProduct(
                product_id="CWF-1",
                product_text=cwf_text,
                issuance_time="2026-08-18T18:00:00Z",
                product_type="CWF",
                wfo="LWX",
            )

        async def offshore_forecast_product(self, _office: str) -> NWSProduct:
            return NWSProduct(
                product_id="OFF-1",
                product_text=VALID_OFFNT2,
                issuance_time="2026-08-18T18:00:00Z",
                product_type="OFF",
                wfo="KWBC",
            )

    config = SimpleNamespace(
        offnt2=SimpleNamespace(
            enabled=True,
            source_office="KWBC",
            product_type="OFF",
            zones=[("ANZ450", "New Jersey waters")],
            include_synopsis=True,
            max_chars_normal=1200,
            max_chars_heightened=800,
            max_airtime_seconds=90,
            rotate_period_s=1800,
            rotate_step=1,
            defer_in_heightened=True,
        ),
        cwf=SimpleNamespace(enabled=True, offices=["LWX"], max_chars_normal=1200),
        rwr=SimpleNamespace(pressure_cache_hours=3, pressure_trend_threshold_inhg=0.02),
        last_product_max_chars=260,
        obs=SimpleNamespace(aliases={}),
        spc=SimpleNamespace(enabled=False),
        marine_obs=SimpleNamespace(enabled=False),
        hwo=SimpleNamespace(speak_unavailable=True),
    )
    builder = CycleBuilder(
        api=Api(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=config,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(config),
        work_dir=str(tmp_path),
    )

    async def no_optional_segment(*_args, **_kwargs):
        return None

    for method_name in (
        "build_hwo_segment",
        "build_spc_segment",
        "build_zfp_segment",
        "build_fcst_segment",
        "build_obs_segment",
        "build_marine_obs_segment",
    ):
        monkeypatch.setattr(builder, method_name, no_optional_segment)

    segments = asyncio.run(
        builder.build_segments(
            "station",
            "area",
            "disclaimer",
            CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
        )
    )

    cwf = next(segment for segment in segments if segment.key == "cwf")
    offnt2 = next(segment for segment in segments if segment.key == "offnt2")
    assert "A cold front crosses the waters tonight" in cwf.text
    assert "Synopsis." not in offnt2.text
    assert "The forecast for New Jersey waters." in offnt2.text
