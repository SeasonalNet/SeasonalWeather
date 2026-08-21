"""Versioned worker telemetry allowlists."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import final

from seasonalweather.swwp.messages import WorkerTelemetrySample

_NAME = re.compile(r"^[a-z][a-z0-9_.]{2,63}$")
_LABEL = re.compile(r"^[a-z][a-z0-9_.]{1,31}$")
_FORBIDDEN = re.compile(r"(?i)(password|secret|token|api[_-]?key|authorization|credential|raw|text|alert|payload)")


@final
class TelemetryRejection(ValueError):
    """A worker telemetry sample failed the controller-owned allowlist."""

    def __init__(self, reason: str) -> None:
        self.reason: str = reason[:64]
        super().__init__(self.reason)


@dataclass(frozen=True)
@final
class TelemetryRule:
    name: str
    kind: str
    labels: tuple[str, ...]


def _validate_rule_labels(labels: tuple[str, ...]) -> None:
    if labels != tuple(sorted(set(labels))) or any(
        not _LABEL.fullmatch(label) or _FORBIDDEN.search(label) for label in labels
    ):
        raise ValueError("telemetry rule labels must be unique, sorted, and non-sensitive")


def _validate_rule(rule: TelemetryRule) -> None:
    if not _NAME.fullmatch(rule.name) or _FORBIDDEN.search(rule.name):
        raise ValueError("telemetry rule names must be valid and non-sensitive")
    if rule.kind not in {"counter", "gauge"}:
        raise ValueError("telemetry rule kind is unsupported")
    _validate_rule_labels(tuple(rule.labels))


def _validate_sample_value(kind: str, value: float) -> None:
    if not math.isfinite(value):
        raise TelemetryRejection("value_invalid")
    if kind == "counter" and value < 0:
        raise TelemetryRejection("counter_negative")


def _validate_sample_label(key: str, value: str) -> None:
    if not _LABEL.fullmatch(key) or _FORBIDDEN.search(key):
        raise TelemetryRejection("label_name_invalid")
    if not value or len(value) > 64 or any(ord(char) < 0x20 for char in value):
        raise TelemetryRejection("label_value_unbounded")
    if _FORBIDDEN.search(value):
        raise TelemetryRejection("sensitive_value_rejected")


@final
class TelemetryAllowlist:
    """Validate worker samples before they enter controller metrics."""

    schema_version: int = 1

    def __init__(self, rules: tuple[TelemetryRule, ...]) -> None:
        names = tuple(rule.name for rule in rules)
        if names != tuple(sorted(set(names))):
            raise ValueError("telemetry rule names must be unique and sorted")
        for rule in rules:
            _validate_rule(rule)
        self._rules: dict[str, TelemetryRule] = {rule.name: rule for rule in rules}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))

    def validate(self, sample: WorkerTelemetrySample) -> None:
        name = sample.name
        rule = self._rules.get(name)
        if rule is None:
            raise TelemetryRejection("name_not_allowlisted")
        if sample.kind.value != rule.kind:
            raise TelemetryRejection("kind_not_allowlisted")
        _validate_sample_value(rule.kind, sample.value)
        if set(sample.labels) != set(rule.labels):
            raise TelemetryRejection("labels_not_allowlisted")
        for key, raw_value in sample.labels.items():
            _validate_sample_label(key, str(raw_value))

    def validate_many(self, samples: tuple[WorkerTelemetrySample, ...]) -> tuple[int, int]:
        accepted = 0
        rejected = 0
        for sample in samples:
            try:
                self.validate(sample)
            except TelemetryRejection:
                rejected += 1
            else:
                accepted += 1
        return accepted, rejected


def default_telemetry_allowlist() -> TelemetryAllowlist:
    return TelemetryAllowlist(
        (
            TelemetryRule("worker_assignments_active", "gauge", ("queue",)),
            TelemetryRule("worker_assignments_completed_total", "counter", ("outcome", "queue")),
            TelemetryRule("worker_capability_state", "gauge", ("capability", "state")),
            TelemetryRule("worker_heartbeats_total", "counter", ()),
            TelemetryRule("worker_protocol_errors_total", "counter", ("category",)),
        )
    )
