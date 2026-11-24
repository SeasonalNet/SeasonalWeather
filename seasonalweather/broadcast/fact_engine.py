"""Deterministic, side-effect-free comparison of admitted fact evidence."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from uuid import NAMESPACE_URL, uuid5

from .fact_models import (
    ALERT_KINDS,
    AREA_KINDS,
    FORECAST_KINDS,
    RULES,
    BroadcastFact,
    FactKind,
    FactResult,
    FactSample,
    FactStatus,
    FactValue,
    aware,
)

DERIVATION_VERSION = 1
FACT_DECIMAL_CONTEXT = Context(prec=40)


class FactInvariantError(ValueError):
    """Impossible evidence relationship or conflicting immutable fact identity."""


def _valid_number(kind: FactKind, value: Decimal) -> bool:
    if not value.is_finite() or value.copy_abs() > 100_000:
        return False
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or abs(exponent) > 14:
        return False
    return kind not in {FactKind.FORECAST_WIND_INCREASED, FactKind.MARINE_WINDS_INCREASING} or value >= 0


def _valid_value(kind: FactKind, value: FactValue) -> bool:
    if kind in AREA_KINDS:
        return isinstance(value, tuple) and bool(value) and value == tuple(sorted(set(value)))
    if kind in {FactKind.ALERT_EXPIRATION_EXTENDED, FactKind.FORECAST_PRECIP_START_CHANGED}:
        return isinstance(value, datetime) and aware(value)
    return isinstance(value, Decimal) and _valid_number(kind, value)


def _valid_period(kind: FactKind, sample: FactSample) -> bool:
    period = sample.forecast_period
    if period is None:
        return kind not in FORECAST_KINDS
    start, end = period
    return (
        kind in FORECAST_KINDS
        and aware(start)
        and aware(end)
        and start < end
        and sample.measured_at < end
        and sample.valid_until <= end
        and (not isinstance(sample.value, datetime) or start <= sample.value < end)
    )


def _valid_metadata(sample: FactSample) -> bool:
    return (
        bool(sample.identity)
        and bool(sample.areas)
        and sample.areas == tuple(sorted(set(sample.areas)))
        and aware(sample.measured_at)
        and aware(sample.valid_until)
        and sample.measured_at < sample.valid_until
    )


def _validate(kind: FactKind, sample: FactSample) -> None:
    if not (_valid_metadata(sample) and _valid_value(kind, sample.value) and _valid_period(kind, sample)):
        raise FactInvariantError("Invalid admitted fact evidence")
    if kind in ALERT_KINDS and isinstance(sample.value, datetime) and sample.value != sample.valid_until:
        raise FactInvariantError("Alert expiration does not match source validity")
    if kind in AREA_KINDS and sample.value != sample.areas:
        raise FactInvariantError("Alert area does not match source coverage")


def _area_delta(kind: FactKind, current: FactSample, previous: FactSample) -> tuple[str, ...]:
    new, old = set(current.areas), set(previous.areas)
    if kind is FactKind.ALERT_AREA_EXPANDED and old < new:
        return tuple(sorted(new - old))
    if kind is FactKind.ALERT_AREA_CONTRACTED and new < old:
        return tuple(sorted(old - new))
    return ()


def _affected_areas(kind: FactKind, current: FactSample, previous: FactSample) -> tuple[str, ...]:
    if kind in AREA_KINDS:
        return _area_delta(kind, current, previous)
    rule = RULES[kind]
    new_value, old_value = current.value, previous.value
    if isinstance(new_value, datetime) and isinstance(old_value, datetime):
        delta = (new_value - old_value).total_seconds()
        changed = delta > 0 if kind is FactKind.ALERT_EXPIRATION_EXTENDED else abs(delta) >= rule.minimum
    elif isinstance(new_value, Decimal) and isinstance(old_value, Decimal):
        with localcontext(FACT_DECIMAL_CONTEXT):
            changed = (new_value - old_value) * rule.direction >= rule.minimum
    else:
        raise FactInvariantError("Incompatible admitted fact values")
    return current.areas if changed else ()


def _history_conflicts(kind: FactKind, current: FactSample, previous: FactSample) -> bool:
    return (
        previous.identity != current.identity
        or previous.forecast_period != current.forecast_period
        or (previous.areas != current.areas and kind not in AREA_KINDS)
        or current.measured_at < previous.measured_at
        or (previous.measured_at == current.measured_at and current.value != previous.value)
    )


def _mixed_area_change(current: FactSample, previous: FactSample) -> bool:
    new, old = set(current.areas), set(previous.areas)
    return bool(new - old) and bool(old - new)


def _history_status(kind: FactKind, current: FactSample, previous: FactSample) -> FactStatus | None:
    if previous.source_id == current.source_id and previous != current:
        raise FactInvariantError("One archive record supplied different evidence for the same comparison")
    if _history_conflicts(kind, current, previous):
        return FactStatus.CONFLICTING
    gap = current.measured_at - previous.measured_at
    if not gap or gap > RULES[kind].max_gap:
        return FactStatus.INSUFFICIENT_HISTORY
    if kind in ALERT_KINDS and previous.valid_until <= current.measured_at:
        return FactStatus.INSUFFICIENT_HISTORY
    if kind in AREA_KINDS and _mixed_area_change(current, previous):
        return FactStatus.CONFLICTING
    return None


def _utc_sample(sample: FactSample) -> FactSample:
    period = sample.forecast_period
    return replace(
        sample,
        measured_at=sample.measured_at.astimezone(UTC),
        valid_until=sample.valid_until.astimezone(UTC),
        value=sample.value.astimezone(UTC) if isinstance(sample.value, datetime) else sample.value,
        forecast_period=(period[0].astimezone(UTC), period[1].astimezone(UTC)) if period is not None else None,
    )


def compare_fact(kind: FactKind, current: FactSample, previous: FactSample | None, *, as_of: datetime) -> FactResult:
    """Compare one exact pair; evaluation time affects eligibility, not identity."""
    if not aware(as_of):
        raise ValueError("Fact evaluation requires timezone-aware time")
    _validate(kind, current)
    current = _utc_sample(current)
    expires = min(current.valid_until, current.measured_at + RULES[kind].lifetime)
    if not current.measured_at <= as_of < expires:
        return FactResult(FactStatus.EXPIRED)
    if previous is None:
        return FactResult(FactStatus.INSUFFICIENT_HISTORY)
    _validate(kind, previous)
    previous = _utc_sample(previous)
    status = _history_status(kind, current, previous)
    if status is not None:
        return FactResult(status)
    areas = _affected_areas(kind, current, previous)
    if not areas:
        return FactResult(FactStatus.NO_CHANGE)
    key = (
        f"seasonalweather/facts/{DERIVATION_VERSION}/{kind}/{current.source_id}/{previous.source_id}/{current.identity}"
    )
    return FactResult(
        FactStatus.READY,
        BroadcastFact(
            id=uuid5(NAMESPACE_URL, key),
            kind=kind,
            current=current,
            previous=previous,
            areas=areas,
            valid_from=current.measured_at.astimezone(UTC),
            expires_at=expires.astimezone(UTC),
            priority=RULES[kind].priority,
        ),
    )
