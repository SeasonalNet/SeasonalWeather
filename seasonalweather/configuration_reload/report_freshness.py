"""Controller-owned validation-report age and bounded clock-skew policy."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass

from .models import VALIDATION_REPORT_CLOCK_SKEW_SECONDS, VALIDATION_REPORT_MAX_AGE_SECONDS


class ReportFreshnessError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("validation report freshness fence failed")


@dataclass(frozen=True)
class ReportFreshness:
    completed_at: dt.datetime
    expires_at: dt.datetime
    age_seconds: float

    def challenge_fields(self) -> dict[str, object]:
        return {
            "validator_completed_at": self.completed_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "maximum_age_seconds": VALIDATION_REPORT_MAX_AGE_SECONDS,
            "clock_skew_seconds": VALIDATION_REPORT_CLOCK_SKEW_SECONDS,
        }


def require_report_fresh(
    report: Mapping[str, object],
    *,
    now: dt.datetime,
) -> ReportFreshness:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ReportFreshnessError("validation_report_clock_malformed")
    stamp = report.get("validator_stamp")
    if not isinstance(stamp, Mapping):
        raise ReportFreshnessError("validation_report_timestamp_malformed")
    raw_completed = stamp.get("completed_at")
    if not isinstance(raw_completed, str):
        raise ReportFreshnessError("validation_report_timestamp_malformed")
    try:
        completed = dt.datetime.fromisoformat(raw_completed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportFreshnessError("validation_report_timestamp_malformed") from exc
    if completed.tzinfo is None or completed.utcoffset() is None:
        raise ReportFreshnessError("validation_report_timestamp_malformed")
    completed = completed.astimezone(dt.UTC)
    current = now.astimezone(dt.UTC)
    future_skew = (completed - current).total_seconds()
    if future_skew > VALIDATION_REPORT_CLOCK_SKEW_SECONDS:
        raise ReportFreshnessError("validation_report_from_future")
    age = max(0.0, (current - completed).total_seconds())
    if age > VALIDATION_REPORT_MAX_AGE_SECONDS:
        raise ReportFreshnessError("validation_report_expired")
    return ReportFreshness(
        completed_at=completed,
        expires_at=completed + dt.timedelta(seconds=VALIDATION_REPORT_MAX_AGE_SECONDS),
        age_seconds=age,
    )
