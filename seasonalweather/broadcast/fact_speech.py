"""Fixed speech templates; never interpolate untrusted source labels."""

from datetime import UTC, datetime
from decimal import Decimal

from .fact_models import RULES, BroadcastFact, FactKind, FactValue

_NUMERIC = {
    FactKind.FORECAST_WIND_INCREASED: "Forecast wind speed increased",
    FactKind.OBS_TEMPERATURE_FALLING: "Observed temperature fell",
    FactKind.OBS_PRESSURE_FALLING: "Observed pressure fell",
    FactKind.MARINE_WINDS_INCREASING: "Forecast marine wind speed increased",
    FactKind.RIVER_RISING: "River level rose",
}


def number_text(value: Decimal) -> str:
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def canonical_value(value: FactValue) -> str | tuple[str, ...]:
    if isinstance(value, Decimal):
        return number_text(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def _time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d at %H:%M UTC")


def render_fact(fact: BroadcastFact) -> str:
    if fact.kind is FactKind.ALERT_AREA_EXPANDED:
        return "The alert area expanded."
    if fact.kind is FactKind.ALERT_AREA_CONTRACTED:
        return "The alert area was reduced."
    current, previous = fact.current.value, fact.previous.value
    if isinstance(current, datetime) and isinstance(previous, datetime):
        if fact.kind is FactKind.ALERT_EXPIRATION_EXTENDED:
            return f"The alert expiration was extended from {_time(previous)} to {_time(current)}."
        return f"The forecast start of precipitation changed from {_time(previous)} to {_time(current)}."
    if isinstance(current, Decimal) and isinstance(previous, Decimal):
        unit = RULES[fact.kind].unit
        return f"{_NUMERIC[fact.kind]} from {number_text(previous)} to {number_text(current)} {unit}."
    raise ValueError("Fact value cannot be rendered")
