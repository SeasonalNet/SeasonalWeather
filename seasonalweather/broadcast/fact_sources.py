"""Admit recognized archive payloads without assuming their shape from the table alone."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext

from ..database.postgresql_archive import ArchiveOrigin, ArchiveRecord, ArchiveTable
from .fact_engine import FACT_DECIMAL_CONTEXT
from .fact_models import ALERT_KINDS, AREA_KINDS, FORECAST_KINDS, FactKind, FactSample, FactValue, aware


def source_table(kind: FactKind) -> ArchiveTable:
    if kind in ALERT_KINDS:
        return ArchiveTable.ALERT_LIFECYCLE_VERSIONS
    if kind in FORECAST_KINDS:
        return ArchiveTable.FORECAST_SNAPSHOTS
    return ArchiveTable.OBSERVATION_SAMPLES


def _time(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("Missing source time")
    value = datetime.fromisoformat(raw)
    if not aware(value):
        raise ValueError("Source time must include a timezone")
    return value.astimezone(UTC)


def _areas(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or not 0 < len(raw) <= 256:
        raise ValueError("Missing or oversized source area list")
    if not all(isinstance(area, str) and 0 < len(area) <= 64 and area.isascii() and area.isprintable() for area in raw):
        raise ValueError("Invalid source area identifier")
    return tuple(sorted(set(raw)))


def _number(raw: object) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)) or len(str(raw)) > 32:
        raise ValueError("Invalid source quantity")
    number = Decimal(str(raw))
    exponent = number.as_tuple().exponent
    if not isinstance(exponent, int) or number.copy_abs() > 100_000 or abs(exponent) > 12:
        raise ValueError("Source quantity is out of bounds")
    return number


def _alert(kind: FactKind, record: ArchiveRecord, identity: str) -> FactSample | None:
    rows = record.payload.get("active_alerts")
    if record.origin is not ArchiveOrigin.LOCAL_OUTBOX_REPLAY or not isinstance(rows, list) or len(rows) > 1024:
        return None
    matches = [row for row in rows if isinstance(row, dict) and row.get("id") == identity]
    if len(matches) != 1:
        return None
    row = matches[0]
    areas = _areas(row.get("same_locs"))
    expires = _time(row.get("expires"))
    return FactSample(
        source_id=record.id,
        identity=identity,
        measured_at=_time(row.get("issued")),
        valid_until=expires,
        areas=areas,
        value=areas if kind in AREA_KINDS else expires,
    )


def _observation(kind: FactKind, record: ArchiveRecord, identity: str) -> FactSample | None:
    props = record.payload.get("properties")
    if not isinstance(props, dict) or kind is FactKind.RIVER_RISING:
        return None
    station = props.get("station")
    if not isinstance(station, str) or station != f"https://api.weather.gov/stations/{identity}":
        return None
    temperature = kind is FactKind.OBS_TEMPERATURE_FALLING
    value = props.get("temperature" if temperature else "seaLevelPressure")
    if not isinstance(value, dict) or value.get("unitCode") != ("wmoUnit:degC" if temperature else "wmoUnit:Pa"):
        return None
    raw = _number(value.get("value"))
    observed = _time(props.get("timestamp"))
    with localcontext(FACT_DECIMAL_CONTEXT):
        canonical = raw * Decimal("1.8") + 32 if temperature else raw / 100
    return FactSample(
        source_id=record.id,
        identity=identity,
        measured_at=observed,
        valid_until=observed + timedelta(hours=1),
        areas=(identity,),
        value=canonical,
    )


def _normalized(kind: FactKind, record: ArchiveRecord, identity: str, raw: dict[str, object]) -> FactSample | None:
    if raw.get("identity") != identity or raw.get("version") != 1 or raw.get("kind") != kind.value:
        return None
    areas = _areas(raw.get("areas"))
    expires = _time(raw.get("valid_until"))
    period = (_time(raw.get("period_start")), _time(raw.get("period_end"))) if kind in FORECAST_KINDS else None
    value = raw.get("value")
    admitted_value: FactValue
    if kind in AREA_KINDS:
        admitted_value = _areas(value)
    elif kind in {FactKind.FORECAST_PRECIP_START_CHANGED, FactKind.ALERT_EXPIRATION_EXTENDED}:
        admitted_value = _time(value)
    else:
        admitted_value = _number(value)
    return FactSample(
        source_id=record.id,
        identity=identity,
        measured_at=_time(raw.get("measured_at")),
        valid_until=expires,
        areas=areas,
        value=admitted_value,
        forecast_period=period,
    )


def normalize_source(kind: FactKind, record: ArchiveRecord, identity: str) -> FactSample | None:
    """Return no evidence for unsupported, malformed, or unrecognized archive payloads."""
    if record.table is not source_table(kind) or record.payload_version != 1:
        return None
    try:
        raw = record.payload.get("fact_evidence")
        if isinstance(raw, dict):
            return _normalized(kind, record, identity, raw)
        if kind in ALERT_KINDS:
            return _alert(kind, record, identity)
        if record.table is ArchiveTable.OBSERVATION_SAMPLES:
            return _observation(kind, record, identity)
    except (ValueError, InvalidOperation, OverflowError):
        return None
    return None
