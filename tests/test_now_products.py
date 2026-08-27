from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from seasonalweather.alerts.nws_api import NWSApi, NWSProduct, NWSProductReference
from seasonalweather.alerts.product import parse_product_text
from seasonalweather.broadcast.now import build_now_script, extract_now_narrative
from seasonalweather.broadcast.now_runtime import NowRuntime
from seasonalweather.config import load_config
from seasonalweather.database.core import SeasonalDatabase
from seasonalweather.database.inserts import CycleInsertRepository


NOWLWX = """607
FPUS71 KLWX 050008
NOWLWX

Short Term Forecast
National Weather Service Baltimore MD/Washington DC
808 PM EDT Sat Jul 4 2026

VAZ057-050045-
King George-
808 PM EDT Sat Jul 4 2026

.NOW...

At 808 PM EDT, Doppler radar indicated a strong thunderstorm near
Port Royal, or near King George, moving east at 35 mph.

Gusty winds of 40 to 50 mph and small hail are possible with this
storm.

Locations impacted include...
King George, Dahlgren, Rollins Fork, Weedonville, Ninde, Lambs Creek,
Jersey, Berthaville, Shiloh, and Dogue.

LAT...LON 3816 7709 3815 7712 3817 7715 3816 7717
      3820 7722 3819 7725 3823 7725 3824 7722
      3825 7723 3825 7727 3822 7729 3831 7730
      3835 7704 3833 7702 3831 7703 3827 7700
      3826 7705 3818 7705 3814 7707
TIME...MOT...LOC 0008Z 275DEG 30KT 3822 7722
$$
Manning
"""


class _TargetResolver:
    def __init__(self, *, in_area: bool = True) -> None:
        self.in_area = in_area
        self.expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=35)

    async def _nwws_same_targets_from_texts(self, primary: str, secondary: str):
        same = ["051099"] if self.in_area else []
        return ["VAZ057"], same, "raw", True, self.expires


class _Conductor:
    def __init__(self) -> None:
        self.notifications = 0

    def notify_inserts_changed(self) -> None:
        self.notifications += 1


def _host(tmp_path: Path, *, in_area: bool = True):
    db = SeasonalDatabase(path=str(tmp_path / "state.sqlite3"))
    return SimpleNamespace(
        cfg=SimpleNamespace(
            now=SimpleNamespace(
                enabled=True,
                intro="A statement from the National Weather Service.",
                default_expire_minutes=60,
                api_backfill=SimpleNamespace(
                    enabled=True,
                    initial_delay_seconds=0,
                    interval_seconds=120,
                    lookback_minutes=120,
                    max_products_per_office=25,
                ),
            ),
            paths=SimpleNamespace(audio_dir=str(tmp_path / "audio")),
            audio=SimpleNamespace(sample_rate=8000),
        ),
        cycle_insert_repo=CycleInsertRepository(db),
        target_resolver=_TargetResolver(in_area=in_area),
        conductor=_Conductor(),
        tts=object(),
        formatters=SimpleNamespace(now_script=build_now_script),
        _nwws_allowed_wfos={"KLWX"},
    )


def test_now_narrative_extracts_only_spoken_body() -> None:
    body = extract_now_narrative(NOWLWX)
    assert body.startswith("At 808 PM EDT, Doppler radar indicated")
    assert "Locations impacted include:" in body
    assert "King George, Dahlgren" in body
    assert "LAT...LON" not in body
    assert "TIME...MOT...LOC" not in body
    assert "Manning" not in body
    assert "VAZ057" not in body

    script = build_now_script(
        NOWLWX,
        intro="A statement from the National Weather Service.",
    )
    assert script.startswith("A statement from the National Weather Service.")
    assert "At 8:08 PM EDT" in script
    assert "Gusty winds of 40 to 50 mph" in script
    assert "LAT...LON" not in script
    assert "3820 7722" not in script


def test_now_narrative_fails_closed_without_now_marker() -> None:
    malformed = NOWLWX.replace(".NOW...", "")
    assert extract_now_narrative(malformed) == ""
    assert build_now_script(malformed, intro="A statement.") == ""


def test_now_runtime_queues_persistent_expiring_routine_insert(tmp_path, monkeypatch) -> None:
    host = _host(tmp_path)
    render_calls: list[tuple[str, Path]] = []

    def _fake_render(_tts, text, output_path, *, sample_rate, seg_gap_s=0.45):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF-test")
        render_calls.append((text, path))
        return 12.5

    monkeypatch.setattr("seasonalweather.broadcast.now_runtime.render_segment_wav", _fake_render)
    parsed = parse_product_text(NOWLWX)
    assert parsed is not None

    assert asyncio.run(NowRuntime(host).handle(parsed)) is True

    active = host.cycle_insert_repo.list_inserts()
    assert len(active) == 1
    item = active[0]
    assert item["kind"] == "text"
    assert item["title"] == "Short-Term Forecast."
    assert item["placement"] == "after_status"
    assert item["repeat_mode"] == "every_n_rotations"
    assert item["repeat_every_rotations"] == 1
    assert item["defer_during_active_alerts"] is False
    assert item["status"] == "active"
    assert item["meta"]["wfo"] == "KLWX"
    assert item["meta"]["source_type"] == "nws_now"
    assert item["meta"]["ugc_zones"] == ["VAZ057"]
    assert item["meta"]["same_locations"] == ["051099"]
    assert "At 8:08 PM EDT" in item["text"]
    assert "LAT...LON" not in item["text"]
    assert Path(item["audio_path"]).exists()
    assert len(render_calls) == 1
    assert host.conductor.notifications == 1

    # A duplicate NWWS delivery reuses the existing rendered audio.
    assert asyncio.run(NowRuntime(host).handle(parsed)) is True
    assert len(render_calls) == 1
    assert host.conductor.notifications == 2

    due_in_focus = host.cycle_insert_repo.list_due(
        placement="after_status",
        rotation_count=1,
        now_iso=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        active_alert_focus=True,
    )
    assert [x["insert_id"] for x in due_in_focus] == [item["insert_id"]]


def test_now_out_of_area_update_cancels_overlapping_prior_insert(tmp_path, monkeypatch) -> None:
    host = _host(tmp_path, in_area=True)

    def _fake_render(_tts, text, output_path, *, sample_rate, seg_gap_s=0.45):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF-test")
        return 5.0

    monkeypatch.setattr("seasonalweather.broadcast.now_runtime.render_segment_wav", _fake_render)
    parsed = parse_product_text(NOWLWX)
    assert parsed is not None
    runtime = NowRuntime(host)

    assert asyncio.run(runtime.handle(parsed)) is True
    host.target_resolver.in_area = False
    assert asyncio.run(runtime.handle(parsed)) is False

    items = host.cycle_insert_repo.list_inserts(include_inactive=True)
    assert len(items) == 1
    assert items[0]["status"] == "cancelled"
    assert host.conductor.notifications == 2


def test_now_runtime_rejects_delayed_older_overlapping_product(tmp_path, monkeypatch) -> None:
    host = _host(tmp_path, in_area=True)
    render_calls = 0

    def _fake_render(_tts, text, output_path, *, sample_rate, seg_gap_s=0.45):
        nonlocal render_calls
        render_calls += 1
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF-test")
        return 5.0

    monkeypatch.setattr("seasonalweather.broadcast.now_runtime.render_segment_wav", _fake_render)
    current = parse_product_text(NOWLWX)
    assert current is not None
    runtime = NowRuntime(host)
    assert asyncio.run(runtime.handle(current)) is True

    older_text = (
        NOWLWX.replace("808 PM EDT Sat Jul 4 2026", "758 PM EDT Sat Jul 4 2026")
        .replace("At 808 PM EDT", "At 758 PM EDT")
        .replace("strong thunderstorm", "older strong thunderstorm")
    )
    older = parse_product_text(older_text)
    assert older is not None
    assert asyncio.run(runtime.handle(older)) is False

    active = host.cycle_insert_repo.list_inserts()
    assert len(active) == 1
    assert "older strong thunderstorm" not in str(active[0]["text"])
    assert render_calls == 1
    assert host.conductor.notifications == 1


def test_now_config_defaults(monkeypatch) -> None:
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "test-source")
    monkeypatch.setenv("NWWS_JID", "changeme@nwws-oi.weather.gov")
    monkeypatch.setenv("NWWS_PASSWORD", "CHANGEME")
    cfg = load_config("config/config.yaml")
    assert cfg.now.enabled is True
    assert cfg.now.intro == "A statement from the National Weather Service."
    assert cfg.now.default_expire_minutes == 60
    assert cfg.now.api_backfill.enabled is True
    assert cfg.now.api_backfill.initial_delay_seconds == 15
    assert cfg.now.api_backfill.interval_seconds == 120
    assert cfg.now.api_backfill.lookback_minutes == 120
    assert cfg.now.api_backfill.max_products_per_office == 25


def test_nws_product_index_is_sorted_newest_first() -> None:
    api = object.__new__(NWSApi)

    async def _fake_get_json(url, params=None):
        assert url.endswith("/products/types/NOW/locations/LWX")
        return {
            "@graph": [
                {
                    "@id": "https://api.weather.gov/products/older",
                    "issuanceTime": "2026-07-04T22:41:00+00:00",
                    "productCode": "NOW",
                    "issuingOffice": "KLWX",
                },
                {
                    "id": "newer",
                    "issuanceTime": "2026-07-05T00:08:00+00:00",
                    "productCode": "NOW",
                    "issuingOffice": "KLWX",
                },
            ]
        }

    api._get_json = _fake_get_json
    refs = asyncio.run(api.list_product_references("now", "lwx"))
    assert [ref.product_id for ref in refs] == ["newer", "older"]
    assert refs[0].wfo == "KLWX"
    assert asyncio.run(api.latest_product_id("NOW", "LWX")) == "newer"


class _BackfillApi:
    def __init__(self, refs, products) -> None:
        self.refs = refs
        self.products = products
        self.index_calls: list[tuple[str, str, int]] = []
        self.product_calls: list[str] = []

    async def list_product_references(self, product_type, office, *, limit=None):
        self.index_calls.append((product_type, office, limit))
        return list(self.refs)[:limit]

    async def get_product(self, product_id):
        self.product_calls.append(product_id)
        return self.products.get(product_id)


def test_now_api_backfill_queues_all_recent_products_once(tmp_path) -> None:
    host = _host(tmp_path)
    now_utc = dt.datetime.now(dt.timezone.utc)
    recent_one = NWSProductReference(
        product_id="recent-one",
        issuance_time=(now_utc - dt.timedelta(minutes=5)).isoformat(),
        product_type="NOW",
        wfo="KLWX",
    )
    recent_two = NWSProductReference(
        product_id="recent-two",
        issuance_time=(now_utc - dt.timedelta(minutes=20)).isoformat(),
        product_type="NOW",
        wfo="KLWX",
    )
    old = NWSProductReference(
        product_id="old",
        issuance_time=(now_utc - dt.timedelta(hours=3)).isoformat(),
        product_type="NOW",
        wfo="KLWX",
    )
    host.api = _BackfillApi(
        [recent_one, recent_two, old],
        {
            "recent-one": NWSProduct("recent-one", NOWLWX),
            "recent-two": NWSProduct("recent-two", NOWLWX),
            "old": NWSProduct("old", NOWLWX),
        },
    )
    runtime = NowRuntime(host)

    assert asyncio.run(runtime.backfill_recent_once()) == 2
    assert runtime._queue.qsize() == 2
    assert host.api.index_calls == [("NOW", "LWX", 25)]
    assert host.api.product_calls == ["recent-one", "recent-two"]

    # Both IDs are already queued, so the next poll neither re-fetches nor
    # re-enqueues them during this service lifetime.
    assert asyncio.run(runtime.backfill_recent_once()) == 0
    assert runtime._queue.qsize() == 2
    assert host.api.product_calls == ["recent-one", "recent-two"]


def test_now_api_source_is_recorded_without_changing_cycle_semantics(tmp_path, monkeypatch) -> None:
    host = _host(tmp_path)

    def _fake_render(_tts, text, output_path, *, sample_rate, seg_gap_s=0.45):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF-test")
        return 5.0

    monkeypatch.setattr("seasonalweather.broadcast.now_runtime.render_segment_wav", _fake_render)
    parsed = parse_product_text(NOWLWX)
    assert parsed is not None

    runtime = NowRuntime(host)
    assert asyncio.run(
        runtime.handle(
            parsed,
            source="api-backfill",
            product_id="afd94e37-bf21-4f6d-90bd-0f5345efae03",
        )
    ) is True

    item = host.cycle_insert_repo.list_inserts()[0]
    assert item["actor"] == "api-backfill:KLWX"
    assert item["meta"]["source"] == "api.weather.gov"
    assert item["meta"]["api_product_id"] == "afd94e37-bf21-4f6d-90bd-0f5345efae03"
    assert item["defer_during_active_alerts"] is False
