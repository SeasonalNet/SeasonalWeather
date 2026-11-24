from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

import pytest

from seasonalweather.broadcast.fact_engine import FactInvariantError, compare_fact
from seasonalweather.broadcast.fact_models import (
    ALERT_KINDS,
    FORECAST_KINDS,
    FactKind,
    FactSample,
    FactStatus,
    FactValue,
)
from seasonalweather.broadcast.fact_service import BroadcastFactService
from seasonalweather.broadcast.fact_sources import normalize_source, source_table
from seasonalweather.broadcast.fact_speech import render_fact
from seasonalweather.database.fact_storage import ArchiveRef, CandidateConflictError
from seasonalweather.database.geographic import GeographicValidationUnavailable
from seasonalweather.database.postgresql_archive import ArchiveOrigin, ArchiveRecord

NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)
A, B = UUID(int=1), UUID(int=2)


@pytest.mark.parametrize(
    ("kind", "old_value", "new_value", "speech"),
    [
        (FactKind.ALERT_AREA_EXPANDED, ("A",), ("A", "B"), "The alert area expanded."),
        (FactKind.ALERT_AREA_CONTRACTED, ("A", "B"), ("A",), "The alert area was reduced."),
        (
            FactKind.ALERT_EXPIRATION_EXTENDED,
            NOW + HOUR,
            NOW + 2 * HOUR,
            "The alert expiration was extended from 2026-11-24 at 13:00 UTC to 2026-11-24 at 14:00 UTC.",
        ),
        (
            FactKind.FORECAST_PRECIP_START_CHANGED,
            NOW + HOUR,
            NOW + 2 * HOUR,
            "The forecast start of precipitation changed from 2026-11-24 at 13:00 UTC to 2026-11-24 at 14:00 UTC.",
        ),
        (
            FactKind.FORECAST_WIND_INCREASED,
            Decimal(10),
            Decimal(15),
            "Forecast wind speed increased from 10 to 15 miles per hour.",
        ),
        (
            FactKind.OBS_TEMPERATURE_FALLING,
            Decimal(43),
            Decimal(40),
            "Observed temperature fell from 43 to 40 degrees Fahrenheit.",
        ),
        (
            FactKind.OBS_PRESSURE_FALLING,
            Decimal(1012),
            Decimal(1010),
            "Observed pressure fell from 1012 to 1010 hectopascals.",
        ),
        (
            FactKind.MARINE_WINDS_INCREASING,
            Decimal(10),
            Decimal(15),
            "Forecast marine wind speed increased from 10 to 15 knots.",
        ),
        (FactKind.RIVER_RISING, Decimal(10), Decimal("10.50"), "River level rose from 10 to 10.5 feet."),
    ],
)
def test_typed_fact_and_speech(kind: FactKind, old_value: FactValue, new_value: FactValue, speech: str) -> None:
    current = _sample(kind, A, NOW, new_value)
    previous = _sample(kind, B, NOW - HOUR, old_value)
    result = compare_fact(kind, current, previous, as_of=NOW)
    assert result.fact is not None and result.status is FactStatus.READY
    assert result.fact.areas == (("B",) if isinstance(new_value, tuple) else ("A",))
    assert render_fact(result.fact) == speech
    assert compare_fact(kind, current, previous, as_of=NOW + timedelta(minutes=1)) == result
    assert compare_fact(kind, current, previous, as_of=result.fact.expires_at).status is FactStatus.EXPIRED
    assert compare_fact(kind, current, previous, as_of=NOW - HOUR).status is FactStatus.EXPIRED
    assert compare_fact(kind, current, None, as_of=NOW).status is FactStatus.INSUFFICIENT_HISTORY
    unchanged = _sample(kind, B, NOW - HOUR, new_value)
    assert compare_fact(kind, current, unchanged, as_of=NOW).status is FactStatus.NO_CHANGE


def _sample(kind: FactKind, source: UUID, at: datetime, value: FactValue) -> FactSample:
    return FactSample(
        source_id=source,
        identity="TEST",
        measured_at=at,
        valid_until=value if isinstance(value, datetime) and kind in ALERT_KINDS else NOW + 3 * HOUR,
        areas=value if isinstance(value, tuple) else ("A",),
        value=value,
        forecast_period=(NOW, NOW + 4 * HOUR) if kind in FORECAST_KINDS else None,
    )


def test_conflicts_gap_boundary_and_immutability() -> None:
    kind = FactKind.RIVER_RISING
    current = _sample(kind, A, NOW, Decimal(5))
    previous = _sample(kind, B, NOW - 3 * HOUR, Decimal(4))
    assert compare_fact(kind, current, previous, as_of=NOW).status is FactStatus.READY
    for incompatible in (
        replace(previous, identity="OTHER"),
        replace(previous, measured_at=NOW),
        replace(previous, measured_at=NOW + HOUR),
        replace(previous, areas=("B",)),
    ):
        assert compare_fact(kind, current, incompatible, as_of=NOW).status is FactStatus.CONFLICTING
    stale = replace(previous, measured_at=previous.measured_at - timedelta(microseconds=1))
    assert compare_fact(kind, current, stale, as_of=NOW).status is FactStatus.INSUFFICIENT_HISTORY
    assert compare_fact(kind, current, replace(previous, value=Decimal(6)), as_of=NOW).status is FactStatus.NO_CHANGE
    with pytest.raises(FactInvariantError):
        _ = compare_fact(kind, current, replace(previous, source_id=A), as_of=NOW)


def test_mixed_alert_area_change_is_not_an_expansion_or_contraction() -> None:
    for kind in (FactKind.ALERT_AREA_EXPANDED, FactKind.ALERT_AREA_CONTRACTED):
        current = _sample(kind, A, NOW, ("A", "B"))
        previous = _sample(kind, B, NOW - HOUR, ("A", "C"))
        assert compare_fact(kind, current, previous, as_of=NOW).status is FactStatus.CONFLICTING


def _record(kind: FactKind, source: UUID, payload: dict[str, object]) -> ArchiveRecord:
    return ArchiveRecord(
        table=source_table(kind), id=source, origin=ArchiveOrigin.LOCAL_OUTBOX_REPLAY, recorded_at=NOW, payload=payload
    )


def _obs(kind: FactKind, source: UUID, at: datetime, value: int) -> ArchiveRecord:
    return _record(
        kind,
        source,
        {
            "properties": {
                "station": "https://api.weather.gov/stations/TEST",
                "timestamp": at.isoformat(),
                "temperature": {"unitCode": "wmoUnit:degC", "value": value},
            }
        },
    )


def test_archive_computation_replay_and_diagnostic_boundaries() -> None:
    kind = FactKind.OBS_TEMPERATURE_FALLING
    records = {A: _obs(kind, A, NOW, 5), B: _obs(kind, B, NOW - HOUR, 7)}
    repo, diagnostics = Mock(), Mock()
    repo.lookup.side_effect = lambda ref: records[ref.id]
    service = BroadcastFactService(repo, diagnostics=diagnostics)
    current, previous = ArchiveRef(source_table(kind), A), ArchiveRef(source_table(kind), B)
    assert service.compute(kind, "TEST", current, None, as_of=NOW).status is FactStatus.INSUFFICIENT_HISTORY
    assert diagnostics.emit.call_args.args == ("SWFACT4001",)
    diagnostics.reset_mock()
    first = service.compute(kind, "TEST", current, previous, as_of=NOW)
    assert first == service.compute(kind, "TEST", current, previous, as_of=NOW.astimezone(timezone(-HOUR)))
    assert repo.store.call_args_list[0] == repo.store.call_args_list[1]
    row = repo.store.call_args.args[0]
    assert (row.current, row.previous) == (current, previous)
    assert row.payload["speech"] == "Observed temperature fell from 44.6 to 41 degrees Fahrenheit."
    records[B] = _obs(kind, B, NOW - HOUR, 5)
    assert service.compute(kind, "TEST", current, previous, as_of=NOW).status is FactStatus.NO_CHANGE
    for error in (OSError("database"), GeographicValidationUnavailable("geography"), TimeoutError("source")):
        repo.lookup.side_effect = error
        with pytest.raises(type(error)) as caught:
            _ = service.compute(kind, "TEST", current, previous, as_of=NOW)
        assert caught.value is error
    diagnostics.emit.assert_not_called()
    repo.lookup.side_effect = None
    repo.lookup.return_value = None
    with pytest.raises(FactInvariantError):
        _ = service.compute(kind, "TEST", current, previous, as_of=NOW)
    assert diagnostics.emit.call_args.args == ("SWFACT5001",)
    repo.lookup.side_effect = (records[A], _obs(kind, B, NOW - HOUR, 7))
    repo.store.side_effect = CandidateConflictError("retry mismatch")
    with pytest.raises(CandidateConflictError):
        _ = service.compute(kind, "TEST", current, previous, as_of=NOW)
    assert diagnostics.emit.call_args.args == ("SWFACT5001",)


def test_admission_does_not_treat_zone_metadata_as_forecast() -> None:
    kind = FactKind.FORECAST_WIND_INCREASED
    repo, diagnostics = Mock(), Mock()
    repo.lookup.return_value = _record(kind, A, {"properties": {"id": "TEST"}})
    result = BroadcastFactService(repo, diagnostics=diagnostics).compute(
        kind,
        "TEST",
        ArchiveRef(source_table(kind), A),
        None,
        as_of=NOW,
    )
    assert result.status is FactStatus.UNSUPPORTED
    diagnostics.emit.assert_not_called()
    repo.store.assert_not_called()


def test_normalized_evidence_and_alert_snapshot_admission() -> None:
    kind = FactKind.RIVER_RISING
    normalized = _record(
        kind,
        A,
        {
            "fact_evidence": {
                "version": 1,
                "kind": kind.value,
                "identity": "TEST",
                "areas": ["A"],
                "measured_at": NOW.isoformat(),
                "valid_until": (NOW + HOUR).isoformat(),
                "value": "5.5",
            }
        },
    )
    sample = normalize_source(kind, normalized, "TEST")
    assert sample is not None and sample.value == Decimal("5.5")
    assert normalize_source(kind, normalized, "OTHER") is None
    kind = FactKind.ALERT_AREA_EXPANDED
    alert = _record(
        kind,
        B,
        {
            "active_alerts": [
                {
                    "id": "TEST",
                    "same_locs": ["B", "A"],
                    "issued": NOW.isoformat(),
                    "expires": (NOW + HOUR).isoformat(),
                }
            ]
        },
    )
    sample = normalize_source(kind, alert, "TEST")
    assert sample is not None and sample.value == ("A", "B")
