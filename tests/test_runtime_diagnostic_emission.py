from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from seasonalweather.alerts.active import AlertTracker
from seasonalweather.alerts.cap_nws import NwsCapPoller
from seasonalweather.alerts.ipaws_cap import IpawsCapPoller
from seasonalweather.broadcast import ern_gwes
from seasonalweather.broadcast.cycle import CycleBuilder, CycleContext
from seasonalweather.broadcast.ern_gwes import ErnGwesMonitor
from seasonalweather.broadcast.segment_refresher import SegmentRefresher
from seasonalweather.broadcast.segment_registry import DEFAULT_SEGMENT_REGISTRY
from seasonalweather.broadcast.segment_store import SegmentStore
from seasonalweather.database import SeasonalDatabase
from seasonalweather.diagnostics.bindings import FOUNDATION_CODES, SEGMENT_CODES
from seasonalweather.runtime_diagnostics.models import CorrelationContext, DiagnosticRole
from seasonalweather.runtime_diagnostics.repository import OccurrenceRepository
from seasonalweather.runtime_diagnostics.service import RuntimeDiagnosticService
from seasonalweather.runtime_diagnostics.sink import RuntimeDiagnosticSink
from seasonalweather.tts.tts import TTS


class DiagnosticCapture:
    def __init__(self) -> None:
        self.codes: list[str] = []

    def emit(self, code: str, **_kwargs: object) -> None:
        self.codes.append(code)


async def _cancel_sleep(_seconds: float) -> None:
    raise asyncio.CancelledError


def test_cap_poll_failure_promotes_source_diagnostic(monkeypatch, tmp_path) -> None:
    capture = DiagnosticCapture()
    poller = NwsCapPoller(
        out_queue=asyncio.Queue(),
        same_fips_allow=[],
        ledger_path=str(tmp_path / "cap-ledger.json"),
        diagnostic_sink=capture,
    )

    async def fail_fetch() -> dict[str, object]:
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(poller, "_fetch_json", fail_fetch)
    monkeypatch.setattr("seasonalweather.alerts.cap_nws.asyncio.sleep", _cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(poller.run_forever())

    assert FOUNDATION_CODES["cap.source_failed"] in capture.codes


def test_ipaws_poll_failure_promotes_source_diagnostic(monkeypatch, tmp_path) -> None:
    capture = DiagnosticCapture()
    poller = IpawsCapPoller(
        out_queue=asyncio.Queue(),
        same_fips_allow=[],
        ledger_path=str(tmp_path / "ipaws-ledger.json"),
        diagnostic_sink=capture,
    )

    async def fail_fetch() -> str:
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(poller, "_fetch_xml", fail_fetch)
    monkeypatch.setattr("seasonalweather.alerts.ipaws_cap.asyncio.sleep", _cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(poller.run_forever())

    assert FOUNDATION_CODES["cap.source_failed"] in capture.codes


def test_ern_decoder_start_failure_promotes_transport_diagnostic(monkeypatch) -> None:
    capture = DiagnosticCapture()
    monitor = ErnGwesMonitor(
        out_queue=asyncio.Queue(),
        same_fips_allow=[],
        url="http://example.invalid/ern",
        diagnostic_sink=capture,
    )

    async def fail_start(*_args, **_kwargs):
        raise RuntimeError("decoder unavailable")

    monkeypatch.setattr(ern_gwes.asyncio, "create_subprocess_exec", fail_start)
    monkeypatch.setattr(ern_gwes.asyncio, "sleep", _cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor.run_forever())

    assert FOUNDATION_CODES["ern.transport_failed"] in capture.codes


def test_runtime_diagnostic_sink_promotes_to_controller_repository(tmp_path) -> None:
    database = SeasonalDatabase(path=str(tmp_path / "diagnostics.sqlite3"))
    repository = OccurrenceRepository(database)
    service = RuntimeDiagnosticService(repository)
    service.initialize()
    sink = RuntimeDiagnosticSink(
        service,
        CorrelationContext(
            role=DiagnosticRole.CONTROLLER,
            instance_id="controller-test",
            component="test",
            build_identity="test-build",
        ),
        codes={"cap.source_failed": FOUNDATION_CODES["cap.source_failed"]},
    )

    sink.emit(
        FOUNDATION_CODES["cap.source_failed"],
        component="cap-poller",
        message="CAP source failed in the test boundary.",
        operational_effect="CAP ingestion is degraded.",
        recovery_action="Retry the bounded source operation.",
    )

    occurrences = repository.active()
    assert len(occurrences) == 1
    assert occurrences[0].code == FOUNDATION_CODES["cap.source_failed"]


def test_segment_refresh_failure_promotes_failure_and_fallback_diagnostics(monkeypatch) -> None:
    capture = DiagnosticCapture()

    async def record_failure(*_args, **_kwargs) -> None:
        return None

    refresher = SegmentRefresher(
        store=cast(SegmentStore, cast(object, SimpleNamespace(record_failure=record_failure))),
        cycle_builder=cast(CycleBuilder, cast(object, SimpleNamespace())),
        tts=cast(TTS, cast(object, SimpleNamespace())),
        alert_tracker=cast(AlertTracker, cast(object, SimpleNamespace())),
        ctx_fn=lambda: CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
        station_name="Test",
        service_area_name="Test area",
        disclaimer="Test disclaimer.",
        tz=ZoneInfo("UTC"),
        sample_rate=8000,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(),
        diagnostic_sink=capture,
    )

    async def fail_dispatch(*_args, **_kwargs) -> None:
        raise RuntimeError("builder unavailable")

    monkeypatch.setattr(refresher, "_dispatch_refresh", fail_dispatch)

    asyncio.run(refresher._refresh_one("status"))

    assert SEGMENT_CODES["refresh_failed"] in capture.codes
    assert SEGMENT_CODES["fallback_used"] in capture.codes
