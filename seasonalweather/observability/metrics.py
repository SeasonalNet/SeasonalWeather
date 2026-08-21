"""Thread-safe, bounded Prometheus text metrics without a runtime dependency."""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import final

from seasonalweather.swwp.messages import WorkerTelemetry, WorkerTelemetrySample

from .telemetry import TelemetryAllowlist, TelemetryRejection, default_telemetry_allowlist

_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]{0,127}$")
_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_FORBIDDEN = re.compile(r"(?i)(password|secret|token|api[_-]?key|authorization|credential)")
_SENSITIVE_VALUE = re.compile(r"(?i)(password|secret|token|api[_-]?key|authorization|credential)\s*=")
_MAX_LABEL_VALUE = 128


class MetricError(ValueError):
    """Raised when a metric or label violates the bounded registry contract."""


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    help: str
    kind: str
    labels: tuple[str, ...]
    buckets: tuple[float, ...] = ()


@final
class MetricsRegistry:
    """A deterministic metric registry with explicit names and label schemas."""

    def __init__(self) -> None:
        self._definitions: dict[str, MetricDefinition] = {}
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[list[int], float, int]] = {}
        self._lock: threading.Lock = threading.Lock()

    def register_counter(self, name: str, help: str, *, labels: Iterable[str] = ()) -> None:
        self._register(MetricDefinition(name, help, "counter", self._labels(labels)))

    def register_gauge(self, name: str, help: str, *, labels: Iterable[str] = ()) -> None:
        self._register(MetricDefinition(name, help, "gauge", self._labels(labels)))

    def register_histogram(
        self,
        name: str,
        help: str,
        *,
        labels: Iterable[str] = (),
        buckets: Iterable[float] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    ) -> None:
        normalized = tuple(sorted(set(float(item) for item in buckets)))
        if not normalized or any(not math.isfinite(item) or item <= 0 for item in normalized):
            raise MetricError("histogram buckets must be finite positive values")
        self._register(MetricDefinition(name, help, "histogram", self._labels(labels), normalized))

    def inc(self, name: str, value: float = 1.0, *, labels: Mapping[str, object] | None = None) -> None:
        if not math.isfinite(value) or value < 0:
            raise MetricError("counter increments must be finite and non-negative")
        definition, key = self._key(name, labels)
        if definition.kind != "counter":
            raise MetricError(f"{name} is not a counter")
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def set(self, name: str, value: float, *, labels: Mapping[str, object] | None = None) -> None:
        if not math.isfinite(value):
            raise MetricError("gauge values must be finite")
        definition, key = self._key(name, labels)
        if definition.kind != "gauge":
            raise MetricError(f"{name} is not a gauge")
        with self._lock:
            self._values[key] = value

    def set_one_hot(self, name: str, label: str, value: str) -> None:
        """Set one labeled gauge state and remove the previously active state."""

        definition = self._definitions.get(name)
        if definition is None or definition.kind != "gauge" or definition.labels != (label,):
            raise MetricError(f"{name} is not a one-label gauge")
        normalized = self._key(name, {label: value})[1]
        with self._lock:
            self._values = {key: current for key, current in self._values.items() if key[0] != name}
            self._values[normalized] = 1.0

    def observe(self, name: str, value: float, *, labels: Mapping[str, object] | None = None) -> None:
        if not math.isfinite(value) or value < 0:
            raise MetricError("histogram observations must be finite and non-negative")
        definition, key = self._key(name, labels)
        if definition.kind != "histogram":
            raise MetricError(f"{name} is not a histogram")
        with self._lock:
            counts, total, observations = self._histograms.get(
                key,
                ([0 for _ in definition.buckets], 0.0, 0),
            )
            for index, bucket in enumerate(definition.buckets):
                if value <= bucket:
                    counts[index] += 1
            self._histograms[key] = (counts, total + value, observations + 1)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._definitions):
                definition = self._definitions[name]
                lines.extend((f"# HELP {name} {definition.help}", f"# TYPE {name} {definition.kind}"))
                keys = sorted(key for key in self._values if key[0] == name)
                for key in keys:
                    lines.append(f"{name}{_render_labels(key[1])} {_number(self._values[key])}")
                histogram_keys = sorted(key for key in self._histograms if key[0] == name)
                for key in histogram_keys:
                    labels, total, observations = self._histograms[key]
                    cumulative_labels = dict(key[1])
                    for bucket, count in zip(definition.buckets, labels, strict=True):
                        cumulative_labels["le"] = _number(bucket)
                        lines.append(f"{name}_bucket{_render_labels(tuple(sorted(cumulative_labels.items())))} {count}")
                    cumulative_labels["le"] = "+Inf"
                    lines.append(
                        f"{name}_bucket{_render_labels(tuple(sorted(cumulative_labels.items())))} {observations}"
                    )
                    lines.append(f"{name}_count{_render_labels(key[1])} {observations}")
                    lines.append(f"{name}_sum{_render_labels(key[1])} {_number(total)}")
        return "\n".join(lines) + "\n"

    def accept_worker_sample(
        self,
        sample: WorkerTelemetrySample,
        *,
        labels: Mapping[str, object] | None = None,
    ) -> None:
        """Apply one already-typed allowlisted worker sample."""

        name = sample.name
        kind = sample.kind.value
        value = sample.value
        merged = dict(labels or {})
        merged.update(sample.labels)
        if kind == "counter":
            self.inc(name, value, labels=merged)
        elif kind == "gauge":
            self.set(name, value, labels=merged)
        else:
            raise MetricError("worker telemetry kind is not supported")

    def _register(self, definition: MetricDefinition) -> None:
        if not _NAME.fullmatch(definition.name) or _FORBIDDEN.search(definition.name):
            raise MetricError("metric names must be valid, bounded, and non-sensitive")
        if not definition.help or len(definition.help) > 256:
            raise MetricError("metric help text is required and bounded")
        with self._lock:
            prior = self._definitions.get(definition.name)
            if prior is not None and prior != definition:
                raise MetricError(f"metric {definition.name} was registered with a different schema")
            self._definitions[definition.name] = definition

    @staticmethod
    def _labels(labels: Iterable[str]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(labels)))
        if len(normalized) > 8 or any(not _LABEL.fullmatch(item) for item in normalized):
            raise MetricError("metric labels must be unique and bounded")
        if any(_FORBIDDEN.search(item) for item in normalized):
            raise MetricError("sensitive metric labels are not permitted")
        return normalized

    def _key(
        self, name: str, labels: Mapping[str, object] | None
    ) -> tuple[MetricDefinition, tuple[str, tuple[tuple[str, str], ...]]]:
        with self._lock:
            try:
                definition = self._definitions[name]
            except KeyError as exc:
                raise MetricError(f"metric {name} is not registered") from exc
        normalized = _normalize_metric_label_values(definition, labels or {})
        return definition, (name, normalized)


@final
class WorkerTelemetryMetricsPort:
    """Controller-side SWWP port that applies only allowlisted worker samples."""

    def __init__(
        self,
        registry: MetricsRegistry,
        *,
        allowlist: TelemetryAllowlist | None = None,
    ) -> None:
        self.registry: MetricsRegistry = registry
        self.allowlist: TelemetryAllowlist = allowlist or default_telemetry_allowlist()

    def handle(
        self,
        telemetry: WorkerTelemetry,
        *,
        worker_id: str,
        worker_instance_id: str,
        session_id: str,
        worker_epoch: int,
    ) -> tuple[int, int, str]:
        worker_labels = {
            "worker_id": worker_id,
            "worker_instance_id": worker_instance_id,
            "swwp_session": session_id,
            "worker_epoch": str(worker_epoch),
        }
        accepted = 0
        rejected = 0
        reasons: set[str] = set()
        for sample in telemetry.samples:
            try:
                self.allowlist.validate(sample)
                self.registry.accept_worker_sample(
                    sample,
                    labels=worker_labels,
                )
            except (MetricError, TelemetryRejection) as exc:
                rejected += 1
                reasons.add(getattr(exc, "reason", "metric_rejected"))
                self.registry.inc(
                    "seasonalweather_worker_telemetry_rejected_total",
                    labels={
                        "reason": getattr(exc, "reason", "metric_rejected")[:64],
                        **worker_labels,
                    },
                )
            else:
                accepted += 1
        summary = "accepted" if not rejected else f"accepted={accepted};rejected={rejected}"
        if reasons:
            summary += ";reasons=" + ",".join(sorted(reasons))[:128]
        return accepted, rejected, summary


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{key}="{_escape(value)}"' for key, value in labels) + "}"


def _normalize_metric_label_values(
    definition: MetricDefinition,
    values: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    if set(values) != set(definition.labels):
        raise MetricError(f"labels for {definition.name} must be exactly {definition.labels}")
    normalized: list[tuple[str, str]] = []
    for key in definition.labels:
        value = str(values[key])
        if not value or len(value) > _MAX_LABEL_VALUE or any(ord(char) < 0x20 for char in value):
            raise MetricError("metric label values must be printable and bounded")
        if _FORBIDDEN.search(key) or _SENSITIVE_VALUE.search(value):
            raise MetricError("sensitive metric labels are not permitted")
        normalized.append((key, value))
    return tuple(normalized)


def _number(value: float) -> str:
    return str(int(value)) if value.is_integer() else format(value, ".12g")


def create_default_metrics() -> MetricsRegistry:
    registry = MetricsRegistry()
    registry.register_counter(
        "seasonalweather_api_requests_total",
        "HTTP requests handled by the controller.",
        labels=("method", "route", "status"),
    )
    registry.register_histogram(
        "seasonalweather_api_request_duration_seconds",
        "Controller HTTP request duration in seconds.",
        labels=("method", "route"),
    )
    registry.register_gauge(
        "seasonalweather_lifecycle_ready",
        "Whether the controller reports broadcast-critical readiness.",
    )
    registry.register_gauge(
        "seasonalweather_lifecycle_state",
        "Current lifecycle state represented by a one-hot state label.",
        labels=("state",),
    )
    registry.register_gauge(
        "seasonalweather_build_info",
        "Build identity and runtime role associated with this metrics registry.",
        labels=("build_id", "role", "instance_id"),
    )
    registry.register_counter(
        "seasonalweather_worker_telemetry_rejected_total",
        "Worker telemetry samples rejected by the controller allowlist.",
        labels=("reason", "worker_id", "worker_instance_id", "swwp_session", "worker_epoch"),
    )
    registry.register_gauge(
        "worker_assignments_active",
        "Active assignments reported by an allowlisted worker.",
        labels=("queue", "worker_id", "worker_instance_id", "swwp_session", "worker_epoch"),
    )
    registry.register_counter(
        "worker_assignments_completed_total",
        "Completed assignments reported by an allowlisted worker.",
        labels=("outcome", "queue", "worker_id", "worker_instance_id", "swwp_session", "worker_epoch"),
    )
    registry.register_counter(
        "worker_heartbeats_total",
        "Heartbeats reported by an allowlisted worker.",
        labels=("worker_id", "worker_instance_id", "swwp_session", "worker_epoch"),
    )
    registry.register_counter(
        "worker_protocol_errors_total",
        "Protocol errors reported by an allowlisted worker.",
        labels=("category", "worker_id", "worker_instance_id", "swwp_session", "worker_epoch"),
    )
    registry.register_gauge(
        "worker_capability_state",
        "Capability state reported by an allowlisted worker.",
        labels=("capability", "state", "worker_id", "worker_instance_id", "swwp_session", "worker_epoch"),
    )
    registry.register_counter(
        "seasonalweather_observability_sink_dropped_total",
        "Observability records dropped because a bounded sink queue was full.",
        labels=("sink",),
    )
    registry.register_counter(
        "seasonalweather_observability_sink_failed_total",
        "Optional observability sink deliveries that failed without blocking callers.",
        labels=("sink",),
    )
    return registry
