from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from seasonalweather.alerts.nws_api import NWSApi, NWSProduct
from seasonalweather.broadcast.cycle import CycleBuilder, CycleContext
from seasonalweather.broadcast.segment_builders import SegmentBuildInput, SegmentCandidate
from seasonalweather.broadcast.segment_registry import ResolvedSegmentRegistry

ROOT = Path(__file__).parents[1]
CWF_FIXTURE = ROOT / "tests/fixtures/cwf_lwx_20260830.txt"


class _CwfApi:
    async def coastal_waters_forecast_product(self, office: str) -> NWSProduct:
        return NWSProduct(
            product_id="cwf-lwx-20260830",
            product_text=CWF_FIXTURE.read_text(encoding="utf-8"),
            issuance_time="2026-08-30T22:20:00+00:00",
            product_type="CWF",
            wfo=office,
        )


class _CwfRegistry:
    def enabled(self, key: str) -> bool:
        return key == "cwf"

    def title_for(self, key: str) -> str:
        return key.upper()


def _cwf_builder() -> CycleBuilder:
    config = SimpleNamespace(
        primary_wfo="LWX",
        cwf=SimpleNamespace(enabled=True, offices=["LWX"], max_chars_normal=2000),
        rwr=SimpleNamespace(pressure_cache_hours=3, pressure_trend_threshold_inhg=0.02),
        obs=SimpleNamespace(aliases={}),
    )
    return CycleBuilder(
        api=cast(NWSApi, cast(object, _CwfApi())),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=config,
        registry=cast(ResolvedSegmentRegistry, cast(object, _CwfRegistry())),
    )


def test_cwf_cycle_trim_stops_at_a_complete_sentence() -> None:
    builder = _cwf_builder()
    candidate = asyncio.run(
        builder.build_cwf_segment(
            SegmentBuildInput(
                key="cwf",
                context=CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
                station_name="station",
                service_area_name="area",
                disclaimer="disclaimer",
            )
        )
    )

    assert candidate is not None
    assert candidate.text.endswith("Monday night.…")
    assert not candidate.text.endswith("Monday night. south…")


def test_segment_candidate_preserves_overlong_text_instead_of_clipping() -> None:
    text = "word " * 3000
    candidate = SegmentCandidate(key="probe", title="Probe", text=text)

    assert candidate.text == text.strip()
