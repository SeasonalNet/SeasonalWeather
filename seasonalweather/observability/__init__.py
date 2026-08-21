"""Bounded application observability primitives."""

from .correlation import CorrelationFields, bind_correlation, current_correlation, set_correlation
from .metrics import MetricError, MetricsRegistry, WorkerTelemetryMetricsPort, create_default_metrics
from .outputs import (
    AlertmanagerTransport,
    OtlpHttpTransport,
    OutputEvent,
    PySnmpV3Transport,
    SnmpV3Transport,
    SyslogTlsTransport,
    build_output_hub,
)
from .sinks import NonBlockingSink, OutputHub, SinkStats
from .telemetry import TelemetryAllowlist, TelemetryRejection, default_telemetry_allowlist
from .tracing import TraceContext, bind_trace_context, current_trace_context

__all__ = [
    "CorrelationFields",
    "MetricsRegistry",
    "MetricError",
    "WorkerTelemetryMetricsPort",
    "NonBlockingSink",
    "OutputHub",
    "SinkStats",
    "OutputEvent",
    "PySnmpV3Transport",
    "SyslogTlsTransport",
    "OtlpHttpTransport",
    "AlertmanagerTransport",
    "SnmpV3Transport",
    "build_output_hub",
    "TelemetryAllowlist",
    "TelemetryRejection",
    "TraceContext",
    "bind_correlation",
    "bind_trace_context",
    "create_default_metrics",
    "current_correlation",
    "current_trace_context",
    "default_telemetry_allowlist",
    "set_correlation",
]
