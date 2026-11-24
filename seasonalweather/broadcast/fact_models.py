"""Normalized evidence for optional, pre-air broadcast facts."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class FactKind(StrEnum):
    ALERT_AREA_EXPANDED = "ALERT_AREA_EXPANDED"
    ALERT_AREA_CONTRACTED = "ALERT_AREA_CONTRACTED"
    ALERT_EXPIRATION_EXTENDED = "ALERT_EXPIRATION_EXTENDED"
    FORECAST_PRECIP_START_CHANGED = "FORECAST_PRECIP_START_CHANGED"
    FORECAST_WIND_INCREASED = "FORECAST_WIND_INCREASED"
    OBS_TEMPERATURE_FALLING = "OBS_TEMPERATURE_FALLING"
    OBS_PRESSURE_FALLING = "OBS_PRESSURE_FALLING"
    MARINE_WINDS_INCREASING = "MARINE_WINDS_INCREASING"
    RIVER_RISING = "RIVER_RISING"


class FactStatus(StrEnum):
    READY = "ready"
    NO_CHANGE = "no_change"
    INSUFFICIENT_HISTORY = "insufficient_history"
    UNSUPPORTED = "unsupported"
    CONFLICTING = "conflicting"
    EXPIRED = "expired"


class FactConfidence(StrEnum):
    COMPARABLE = "comparable"


FactValue = Decimal | datetime | tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class FactSample:
    source_id: UUID
    identity: str
    measured_at: datetime
    valid_until: datetime
    areas: tuple[str, ...]
    value: FactValue
    forecast_period: tuple[datetime, datetime] | None = None


@dataclass(frozen=True, kw_only=True)
class BroadcastFact:
    id: UUID
    kind: FactKind
    current: FactSample
    previous: FactSample
    areas: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime
    priority: int
    confidence: FactConfidence = FactConfidence.COMPARABLE


@dataclass(frozen=True)
class FactResult:
    status: FactStatus
    fact: BroadcastFact | None = None


@dataclass(frozen=True)
class FactRule:
    max_gap: timedelta
    lifetime: timedelta
    minimum: Decimal = Decimal(0)
    direction: int = 1
    unit: str = ""
    priority: int = 30


_ALERT_RULE = FactRule(timedelta(hours=24), timedelta(hours=1), priority=10)
_FORECAST_GAP = timedelta(hours=24)
_FORECAST_LIFE = timedelta(hours=6)
_OBSERVATION_GAP = timedelta(hours=3)
_OBSERVATION_LIFE = timedelta(hours=1)
RULES = {
    FactKind.ALERT_AREA_EXPANDED: _ALERT_RULE,
    FactKind.ALERT_AREA_CONTRACTED: _ALERT_RULE,
    FactKind.ALERT_EXPIRATION_EXTENDED: _ALERT_RULE,
    FactKind.FORECAST_PRECIP_START_CHANGED: FactRule(_FORECAST_GAP, _FORECAST_LIFE, Decimal(3600), priority=20),
    FactKind.FORECAST_WIND_INCREASED: FactRule(
        _FORECAST_GAP, _FORECAST_LIFE, Decimal(5), unit="miles per hour", priority=20
    ),
    FactKind.OBS_TEMPERATURE_FALLING: FactRule(
        _OBSERVATION_GAP, _OBSERVATION_LIFE, Decimal(3), -1, "degrees Fahrenheit"
    ),
    FactKind.OBS_PRESSURE_FALLING: FactRule(_OBSERVATION_GAP, _OBSERVATION_LIFE, Decimal(2), -1, "hectopascals"),
    FactKind.MARINE_WINDS_INCREASING: FactRule(_FORECAST_GAP, _FORECAST_LIFE, Decimal(5), unit="knots", priority=20),
    FactKind.RIVER_RISING: FactRule(_OBSERVATION_GAP, _OBSERVATION_LIFE, Decimal("0.5"), unit="feet"),
}
AREA_KINDS = frozenset({FactKind.ALERT_AREA_EXPANDED, FactKind.ALERT_AREA_CONTRACTED})
FORECAST_KINDS = frozenset(
    {FactKind.FORECAST_PRECIP_START_CHANGED, FactKind.FORECAST_WIND_INCREASED, FactKind.MARINE_WINDS_INCREASING}
)
ALERT_KINDS = AREA_KINDS | {FactKind.ALERT_EXPIRATION_EXTENDED}


def aware(value: datetime) -> bool:
    return value.utcoffset() is not None
