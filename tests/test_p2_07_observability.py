from __future__ import annotations

import io
import json
import logging
import sys
from typing import cast
from unittest.mock import patch

from seasonalweather.diagnostics import load_catalog
from seasonalweather.diagnostics.bindings import OBS_CODES
from seasonalweather.logging_config import _observability_failure_code, setup_logging
from seasonalweather.observability import (
    AlertmanagerTransport,
    MetricsRegistry,
    NonBlockingSink,
    OtlpHttpTransport,
    OutputEvent,
    SnmpV3Transport,
    SyslogTlsTransport,
    WorkerTelemetryMetricsPort,
    build_output_hub,
    create_default_metrics,
)
from seasonalweather.observability.telemetry import TelemetryAllowlist, TelemetryRule
from seasonalweather.swwp.messages import TelemetryKind, WorkerTelemetry, WorkerTelemetrySample


def test_structured_logging_is_json_by_default() -> None:
    stream = io.StringIO()
    with patch.object(sys, "stdout", stream):
        setup_logging()
        logging.getLogger("seasonalweather.observability-test").info(
            "worker event",
            extra={"event": "telemetry", "code": "SWWP1001"},
        )

    record = cast(dict[str, object], json.loads(stream.getvalue()))
    assert record["message"] == "worker event"
    assert record["event"] == "telemetry"
    assert record["code"] == "SWWP1001"
    assert record["service"] == "seasonalweather"


def test_observability_diagnostic_namespace_is_catalogued() -> None:
    codes = {str(definition.code) for definition in load_catalog().definitions}
    assert {"SWOBS2001", "SWOBS3001", "SWOBS4001", "SWOBS6001", "SWOBS7001"} <= codes


def test_optional_output_failures_use_trust_code_for_authorization_errors() -> None:
    assert (
        _observability_failure_code(PermissionError("destination forbidden")) == OBS_CODES["destination_unauthorized"]
    )
    assert _observability_failure_code(OSError("connection reset")) == OBS_CODES["transport_failed"]


def test_metrics_registry_renders_bounded_prometheus_text() -> None:
    registry = MetricsRegistry()
    registry.register_counter("test_events_total", "Bounded test events.", labels=("kind",))
    registry.inc("test_events_total", labels={"kind": "accepted"})

    rendered = registry.render()

    assert "# HELP test_events_total Bounded test events." in rendered
    assert 'test_events_total{kind="accepted"} 1' in rendered
    try:
        registry.inc("test_events_total", labels={"kind": "password=secret"})
    except ValueError:
        pass
    else:
        raise AssertionError("sensitive metric label was accepted")

    try:
        registry.register_counter("invalid-name", "Invalid metric name.")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid metric name was accepted")


def test_metrics_registry_replaces_one_hot_state() -> None:
    registry = MetricsRegistry()
    registry.register_gauge("test_state", "Current test state.", labels=("state",))

    registry.set_one_hot("test_state", "state", "ready")
    registry.set_one_hot("test_state", "state", "draining")

    rendered = registry.render()
    assert 'test_state{state="draining"} 1' in rendered
    assert 'test_state{state="ready"}' not in rendered


def test_worker_telemetry_allowlist_accepts_and_rejects_without_raw_content() -> None:
    registry = create_default_metrics()
    port = WorkerTelemetryMetricsPort(registry)
    telemetry = WorkerTelemetry(
        schema_version=1,
        samples=(
            WorkerTelemetrySample(
                name="worker_heartbeats_total",
                kind=TelemetryKind.COUNTER,
                value=1,
            ),
            WorkerTelemetrySample(
                name="worker_not_declared",
                kind=TelemetryKind.GAUGE,
                value=1,
            ),
        ),
    )

    accepted, rejected, summary = port.handle(
        telemetry,
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        worker_epoch=1,
    )

    assert (accepted, rejected) == (1, 1)
    assert "worker_not_declared" not in summary
    assert (
        'worker_heartbeats_total{swwp_session="session_00000001",worker_epoch="1",'
        'worker_id="worker_00000001",worker_instance_id="instance_00000001"} 1' in registry.render()
    )


def test_worker_telemetry_allowlist_rejects_unsafe_rule_definitions() -> None:
    try:
        TelemetryAllowlist((TelemetryRule("worker_token", "gauge", ()),))
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe telemetry rule was accepted")


def test_nonblocking_sink_drops_when_full_without_waiting() -> None:
    sink = NonBlockingSink[str](lambda _item: None, max_queue=1, name="test")

    assert sink.submit("first") is True
    assert sink.submit("second") is False
    assert sink.stats().dropped == 1
    _ = sink.close()


def test_optional_output_event_is_bounded_and_secret_free() -> None:
    event = OutputEvent(
        event="worker_failure",
        message="worker route failed",
        severity="ERROR",
        attributes=(("component", "worker"),),
        diagnostic_code="SWOBS3001",
    )
    assert event.as_dict()["diagnostic_code"] == "SWOBS3001"

    for attributes in (
        (("password", "not-allowed"),),
        (("component", "token=not-allowed"),),
    ):
        try:
            OutputEvent(event="worker_failure", message="bounded", attributes=attributes)
        except ValueError:
            pass
        else:
            raise AssertionError("secret-bearing optional output attributes were accepted")


def test_optional_transports_build_bounded_payloads() -> None:
    event = OutputEvent(
        event="critical_event",
        message="safe failure",
        severity="CRITICAL",
        attributes=(("component", "controller"),),
        traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        diagnostic_code="SWOBS6001",
    )

    syslog = SyslogTlsTransport("collector.example", 6514)
    assert '"event":"critical_event"' in syslog._message(event)
    otlp = OtlpHttpTransport("https://collector.example")
    assert otlp._record(event)["severityText"] == "CRITICAL"
    alertmanager = AlertmanagerTransport("https://alerts.example")
    assert alertmanager.endpoint.endswith("/api/v2/alerts")

    def encode_snmp(event: OutputEvent) -> bytes:
        _ = event
        return b"bounded-packet"

    snmp = SnmpV3Transport("collector.example", 162, encode_snmp)
    assert snmp.encoder(event) == b"bounded-packet"


def test_output_hub_uses_bounded_nonblocking_delivery() -> None:
    delivered: list[OutputEvent] = []

    def deliver(event: OutputEvent) -> None:
        delivered.append(event)

    hub = build_output_hub(
        {"test": deliver},
        queue_size=2,
    )
    hub.submit(OutputEvent(event="one", message="first"))
    hub.submit(OutputEvent(event="two", message="second"))
    _ = hub.close()
    assert delivered
