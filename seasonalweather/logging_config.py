from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import os
import re
import sys
from typing import TYPE_CHECKING

from .diagnostics.bindings import OBS_CODES
from .observability.correlation import current_correlation, set_correlation
from .observability.outputs import (
    AlertmanagerTransport,
    OtlpHttpTransport,
    OutputEvent,
    OutputTransport,
    PySnmpV3Transport,
    SyslogTlsTransport,
    build_output_hub,
)
from .observability.sinks import OutputHub
from .observability.tracing import current_trace_context

if TYPE_CHECKING:
    from .build_metadata import BuildInfo
    from .config import AppConfig, LogsRuntimeConfig
    from .observability.metrics import MetricsRegistry


_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
_VALID_COLOR_MODES = {"never", "auto", "always"}
_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_LEVEL_COLORS = {
    "DEBUG": "\x1b[2m",
    "INFO": "\x1b[32m",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[1;31m",
}
_LOGGER_COLOR = "\x1b[36m"
_SECRET_LOG_PATTERN = re.compile(
    r"(?i)\b(password|secret|token|api[_-]?key|authorization|webhook)(\s*[=:]\s*)(?:bearer\s+)?[^\s,;]+"
)
_output_hub: OutputHub[OutputEvent] | None = None
_runtime_diagnostic_sink: object | None = None


def set_runtime_diagnostic_sink(sink: object | None) -> None:
    """Attach the controller-owned runtime diagnostic port after startup."""

    global _runtime_diagnostic_sink
    _runtime_diagnostic_sink = sink


def _emit_observability_diagnostic(code: str, destination: str, exception: BaseException | None = None) -> None:
    sink = _runtime_diagnostic_sink
    emit = getattr(sink, "emit", None)
    if not callable(emit):
        return
    try:
        emit(
            code,
            component="observability",
            message=f"Optional observability destination {destination[:64]} encountered a bounded failure.",
            operational_effect="The canonical controller path continues while optional observability is degraded.",
            recovery_action="Inspect the destination configuration and bounded transport health.",
            exception=exception,
            source_id=destination,
        )
    except Exception:
        return


def _observability_failure_code(exception: BaseException) -> str:
    """Classify transport failures at the optional-output authority boundary."""

    text = f"{type(exception).__name__} {exception}".lower()
    if isinstance(exception, PermissionError) or any(
        marker in text for marker in ("unauthorized", "forbidden", "permission denied", "authentication failed")
    ):
        return OBS_CODES["destination_unauthorized"]
    return OBS_CODES["transport_failed"]


def _redact_secret_values(text: str) -> str:
    return _SECRET_LOG_PATTERN.sub(r"\1\2[REDACTED]", text)


class _SecretRedactingFormatter(logging.Formatter):
    """Ensure configured application outputs never contain credential values."""

    def format(self, record: logging.LogRecord) -> str:
        return _redact_secret_values(super().format(record))


class StructuredJsonFormatter(logging.Formatter):
    """Canonical bounded JSON formatter for stdout/stderr and containers."""

    _EXTRA_FIELDS = (
        "event",
        "code",
        "component",
        "state",
        "outcome",
        "reason",
        "status_code",
    )

    def format(self, record: logging.LogRecord) -> str:
        message = _redact_secret_values(record.getMessage())[:4096]
        payload: dict[str, object] = {
            "observed_at": dt.datetime.fromtimestamp(record.created, tz=dt.UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name[:128],
            "message": message,
        }
        payload.update(current_correlation().as_dict())
        for field in self._EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is None:
                continue
            text = _redact_secret_values(str(value))[:256]
            if text:
                payload[field] = text
        if record.exc_info:
            payload["exception"] = _redact_secret_values(self.formatException(record.exc_info))[:16_384]
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class _RuntimeMessageFilter(logging.Filter):
    """Filter routine steady-state log chatter based on config.yaml toggles."""

    def __init__(self, runtime_cfg: LogsRuntimeConfig) -> None:
        super().__init__()
        self._runtime_cfg = runtime_cfg

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()

        if not self._runtime_cfg.cap_poll_summary and message.startswith("CAP poll: emitted "):
            return False
        if not self._runtime_cfg.ipaws_poll_summary and message.startswith("IPAWS poll: emitted="):
            return False

        if not self._runtime_cfg.conductor_alert_push and message.startswith("CycleConductor: → alert_"):
            return False
        if not self._runtime_cfg.conductor_live_time_push and message.startswith("CycleConductor: → time "):
            return False
        if not self._runtime_cfg.conductor_cycle_push and message.startswith("CycleConductor: → "):
            return False

        if not self._runtime_cfg.segment_refresher_synth and (
            message.startswith("SegmentRefresher: synthesising alert segment id=")
            or message.startswith("SegmentRefresher: alert script changed, re-synthesising id=")
            or message.startswith("SegmentRefresher: synthesised key=")
        ):
            return False
        return not (
            not self._runtime_cfg.segment_refresher_alert_lifecycle
            and message.startswith("SegmentRefresher: alert segment expired/cancelled id=")
        )


class _SlixmppOutputContainmentFilter(logging.Filter):
    """Keep dependency wire records out of configured application outputs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.name == "slixmpp" or record.name.startswith("slixmpp."))


class _AnsiFormatter(logging.Formatter):
    """ANSI presentation formatter; never mutates the original LogRecord."""

    def format(self, record: logging.LogRecord) -> str:
        colored = copy.copy(record)
        level_color = _LEVEL_COLORS.get(str(record.levelname).upper(), "")
        if level_color:
            colored.levelname = f"{level_color}{record.levelname}{_RESET}"
        colored.name = f"{_LOGGER_COLOR}{record.name}{_RESET}"
        # Keep the timestamp dim but leave the application message itself clean.
        rendered = _redact_secret_values(super().format(colored))
        if rendered:
            parts = rendered.split(" ", 2)
            if len(parts) >= 2:
                parts[0] = f"{_DIM}{parts[0]}"
                parts[1] = f"{parts[1]}{_RESET}"
                rendered = " ".join(parts)
        return rendered


class _OptionalOutputHandler(logging.Handler):
    """Copy bounded log events to optional transports without blocking emitters."""

    def __init__(self, output_hub: OutputHub[OutputEvent]) -> None:
        super().__init__()
        self._output_hub = output_hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = current_correlation().as_dict()
            fields["logger"] = record.name[:128]
            attributes = tuple((key, value[:256]) for key, value in sorted(fields.items())[:16])
            trace = current_trace_context()
            code = getattr(record, "code", None)
            event = OutputEvent(
                event=_output_event_name(getattr(record, "event", "log_record")),
                message=_redact_secret_values(record.getMessage())[:4096],
                severity=record.levelname.upper() if record.levelname.upper() in _VALID_LEVELS else "INFO",
                attributes=attributes,
                traceparent=trace.as_traceparent() if trace is not None else None,
                diagnostic_code=str(code) if code is not None else None,
            )
            self._output_hub.submit(event)
        except Exception:
            self.handleError(record)


def _output_event_name(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "_", str(value).strip().lower()).strip("._-")[:64]
    return normalized if normalized and normalized[0].isalpha() else f"event_{normalized or 'record'}"


def _normalize_level(value: str | None, *, default: str) -> str:
    level = str(value or default).strip().upper()
    return level if level in _VALID_LEVELS else default


def _normalize_color_mode(value: str | None, *, default: str = "never") -> str:
    mode = str(value or default).strip().lower()
    return mode if mode in _VALID_COLOR_MODES else default


def _should_use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "auto":
        return bool(getattr(sys.stdout, "isatty", lambda: False)())
    return False


def _apply_level(logger_name: str, level_name: str) -> None:
    logging.getLogger(logger_name).setLevel(getattr(logging, level_name, logging.INFO))


def _build_output_hub(
    cfg: AppConfig | None,
    metrics: MetricsRegistry | None,
) -> OutputHub[OutputEvent] | None:
    if cfg is None:
        return None
    configured = cfg.logs.outputs
    transports: dict[str, OutputTransport] = {}
    syslog = configured.syslog_tls
    if syslog.enabled:
        transports["syslog_tls"] = SyslogTlsTransport(
            syslog.host,
            syslog.port,
            ca_file=syslog.ca_file,
            server_name=syslog.server_name,
            timeout_seconds=syslog.timeout_seconds,
        )
    otlp = configured.otlp
    if otlp.enabled:
        transports["otlp"] = OtlpHttpTransport(otlp.endpoint, timeout_seconds=otlp.timeout_seconds)
    alertmanager = configured.alertmanager
    if alertmanager.enabled:
        transports["alertmanager"] = AlertmanagerTransport(
            alertmanager.endpoint,
            timeout_seconds=alertmanager.timeout_seconds,
        )
    snmpv3 = configured.snmpv3
    if snmpv3.enabled:
        transports["snmpv3"] = PySnmpV3Transport(
            snmpv3.host,
            snmpv3.port,
            username=snmpv3.username,
            auth_protocol=snmpv3.auth_protocol,
            privacy_protocol=snmpv3.privacy_protocol,
            auth_secret=os.environ.get(snmpv3.auth_secret_env, ""),
            privacy_secret=os.environ.get(snmpv3.privacy_secret_env, ""),
            timeout_seconds=snmpv3.timeout_seconds,
        )

    def on_drop(name: str) -> None:
        if metrics is not None:
            metrics.inc("seasonalweather_observability_sink_dropped_total", labels={"sink": name})
        _emit_observability_diagnostic(OBS_CODES["sink_degraded"], name)
        _emit_observability_diagnostic(OBS_CODES["queue_dropped"], name)

    def on_failure(name: str, exception: BaseException) -> None:
        if metrics is not None:
            metrics.inc("seasonalweather_observability_sink_failed_total", labels={"sink": name})
        _emit_observability_diagnostic(_observability_failure_code(exception), name, exception)

    if not transports:
        return None
    queue_size = max(
        [
            syslog.queue_size,
            otlp.queue_size,
            alertmanager.queue_size,
            snmpv3.queue_size,
        ]
    )
    return build_output_hub(transports, queue_size=min(queue_size, 10_000), on_drop=on_drop, on_failure=on_failure)


def _logging_context(
    role: str | None,
    instance_id: str | None,
    build_info: BuildInfo | None,
) -> dict[str, object]:
    context: dict[str, object] = {"service": "seasonalweather"}
    if role:
        context["role"] = role
    if instance_id:
        context["instance_id"] = instance_id
    if build_info is not None:
        context["build_id"] = build_info.build_id
        context["build_identity"] = build_info.build_identity
    return context


def _configure_formatters(root_logger: logging.Logger, color_mode: str) -> None:
    formatter: logging.Formatter = (
        _AnsiFormatter(_DEFAULT_FORMAT) if _should_use_color(color_mode) else StructuredJsonFormatter()
    )
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_SlixmppOutputContainmentFilter())


def _configure_optional_output_handler(
    root_logger: logging.Logger,
    output_hub: OutputHub[OutputEvent] | None,
) -> None:
    if output_hub is not None:
        root_logger.addHandler(_OptionalOutputHandler(output_hub))


def _configure_runtime_logging(runtime: LogsRuntimeConfig, root_logger: logging.Logger) -> None:
    runtime_filter = _RuntimeMessageFilter(runtime)
    for handler in root_logger.handlers:
        handler.addFilter(runtime_filter)

    logger_levels = (
        ("httpx2", getattr(runtime, "httpx2_level", getattr(runtime, "httpx_level", "WARNING")), "WARNING"),
        ("httpcore2", getattr(runtime, "httpcore2_level", getattr(runtime, "httpcore_level", "WARNING")), "WARNING"),
        ("uvicorn.access", runtime.uvicorn_access_level, "WARNING"),
        ("uvicorn.error", runtime.uvicorn_error_level, "INFO"),
        ("asyncio", runtime.asyncio_level, "WARNING"),
        ("slixmpp", runtime.slixmpp_level, "WARNING"),
        ("slixmpp.xmlstream", runtime.slixmpp_xmlstream_level, "WARNING"),
    )
    for logger_name, level_name, default in logger_levels:
        _apply_level(logger_name, _normalize_level(level_name, default=default))

    for logger_name, level_name in (runtime.logger_levels or {}).items():
        name = str(logger_name).strip()
        if name:
            _apply_level(name, _normalize_level(level_name, default="INFO"))


def setup_logging(
    cfg: AppConfig | None = None,
    *,
    role: str | None = None,
    instance_id: str | None = None,
    build_info: BuildInfo | None = None,
    metrics: MetricsRegistry | None = None,
) -> None:
    runtime = getattr(getattr(cfg, "logs", None), "runtime", None)
    root_level = _normalize_level(getattr(runtime, "level", None), default="INFO")
    color_mode = _normalize_color_mode(getattr(runtime, "color", None), default="never")

    logging.basicConfig(level=getattr(logging, root_level, logging.INFO), stream=sys.stdout, force=True)

    root_logger = logging.getLogger()
    set_correlation(**_logging_context(role, instance_id, build_info))
    _configure_formatters(root_logger, color_mode)
    global _output_hub
    if _output_hub is not None:
        _output_hub.close()
    try:
        _output_hub = _build_output_hub(cfg, metrics)
    except Exception:
        _output_hub = None
        logging.getLogger("seasonalweather.observability").error(
            "optional observability output configuration was rejected",
            extra={
                "event": "observability_output_configuration_failed",
                "code": OBS_CODES["configuration_rejected"],
            },
        )
    _configure_optional_output_handler(root_logger, _output_hub)

    if runtime is None:
        return

    _configure_runtime_logging(runtime, root_logger)
