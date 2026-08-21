"""Bounded optional observability output transports."""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import json
import re
import socket
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast, final

from .sinks import NonBlockingSink, OutputHub

_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_EVENT = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_FORBIDDEN = re.compile(r"(?i)(password|secret|token|api[_-]?key|authorization|credential|raw|text|payload)")
_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_MAX_ATTRIBUTES = 16
_MAX_VALUE = 256
_MAX_MESSAGE = 4096


@dataclass(frozen=True)
class OutputEvent:
    """Sanitized event shared by optional transports."""

    event: str
    message: str
    severity: str = "INFO"
    attributes: tuple[tuple[str, str], ...] = ()
    traceparent: str | None = None
    diagnostic_code: str | None = None

    def __post_init__(self) -> None:
        _validate_event_identity(self.event, self.message, self.severity)
        _validate_attributes(self.attributes)
        _validate_context(self.traceparent, self.diagnostic_code)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "event": self.event,
            "message": self.message,
            "severity": self.severity,
            "attributes": dict(self.attributes),
        }
        if self.traceparent is not None:
            payload["traceparent"] = self.traceparent
        if self.diagnostic_code is not None:
            payload["diagnostic_code"] = self.diagnostic_code
        return payload


class OutputTransport(Protocol):
    def __call__(self, event: OutputEvent) -> None: ...


class _HttpResponse(Protocol):
    def raise_for_status(self) -> None: ...


class _HttpClient(Protocol):
    def __enter__(self) -> _HttpClient: ...

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None: ...

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: object,
    ) -> _HttpResponse: ...


class _HttpClientFactory(Protocol):
    def __call__(self, *, timeout: float, **kwargs: object) -> _HttpClient: ...


class _SnmpEngine(Protocol):
    def close_dispatcher(self) -> int: ...


class _SnmpTransportTarget(Protocol):
    async def create(
        self,
        address: tuple[str, int],
        *,
        timeout: float,
        retries: int,
    ) -> object: ...


class _SnmpNotification(Protocol):
    def add_varbinds(self, *_varbinds: object) -> object: ...


class _SnmpModule(Protocol):
    USM_AUTH_HMAC96_SHA: object
    USM_AUTH_HMAC192_SHA256: object
    USM_AUTH_HMAC384_SHA512: object
    USM_PRIV_CFB128_AES: object
    USM_PRIV_CFB256_AES: object
    UdpTransportTarget: _SnmpTransportTarget

    def SnmpEngine(self) -> _SnmpEngine: ...

    def UsmUserData(self, *args: object, **kwargs: object) -> object: ...

    def ContextData(self) -> object: ...

    def NotificationType(self, identity: object) -> _SnmpNotification: ...

    def ObjectIdentity(self, value: str) -> object: ...

    def ObjectType(self, identity: object, value: object) -> object: ...

    def OctetString(self, value: str) -> object: ...

    def send_notification(
        self, *args: object, **kwargs: object
    ) -> Awaitable[tuple[object, object, object, object]]: ...


@final
class SyslogTlsTransport:
    """RFC 5424 over TCP/TLS; connection work runs only in a sink thread."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ca_file: str = "",
        server_name: str = "",
        timeout_seconds: float = 5.0,
    ) -> None:
        self.host = _endpoint_host(host)
        self.port = _bounded_port(port)
        self.ca_file = ca_file[:512]
        self.server_name = (server_name or self.host)[:255]
        self.timeout_seconds = min(max(timeout_seconds, 0.1), 30.0)

    def __call__(self, event: OutputEvent) -> None:
        context = ssl.create_default_context(cafile=self.ca_file or None)
        with (
            socket.create_connection((self.host, self.port), timeout=self.timeout_seconds) as raw,
            context.wrap_socket(
                raw,
                server_hostname=self.server_name,
            ) as connection,
        ):
            connection.sendall(self._message(event).encode("utf-8", errors="replace"))

    @staticmethod
    def _message(event: OutputEvent) -> str:
        timestamp = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        payload = json.dumps(event.as_dict(), ensure_ascii=True, separators=(",", ":"))[:_MAX_MESSAGE]
        return f"<134>1 {timestamp} seasonalweather - - - {payload}\n"


@final
class OtlpHttpTransport:
    """Small OTLP/HTTP JSON log exporter with bounded request bodies."""

    def __init__(
        self, endpoint: str, *, timeout_seconds: float = 5.0, headers: Mapping[str, str] | None = None
    ) -> None:
        self.endpoint = _http_endpoint(endpoint, "/v1/logs")
        self.timeout_seconds = min(max(timeout_seconds, 0.1), 30.0)
        self.headers = _headers(headers)

    def __call__(self, event: OutputEvent) -> None:
        body = {
            "resourceLogs": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "seasonalweather"}}]},
                    "scopeLogs": [{"logRecords": [self._record(event)]}],
                }
            ]
        }
        client_factory = _http_client_factory()
        with client_factory(timeout=self.timeout_seconds, follow_redirects=False) as client:
            response = client.post(self.endpoint, headers=self.headers, json=body)
            response.raise_for_status()

    @staticmethod
    def _record(event: OutputEvent) -> dict[str, object]:
        attributes: list[dict[str, object]] = [
            {"key": key, "value": {"stringValue": value}} for key, value in event.attributes
        ]
        record: dict[str, object] = {
            "severityText": event.severity,
            "body": {"stringValue": event.message},
            "attributes": attributes,
        }
        if event.traceparent is not None:
            attributes.append({"key": "traceparent", "value": {"stringValue": event.traceparent}})
        if event.diagnostic_code is not None:
            attributes.append({"key": "diagnostic.code", "value": {"stringValue": event.diagnostic_code}})
        return record


@final
class AlertmanagerTransport:
    """Alertmanager v2 event transport for bounded critical notifications."""

    def __init__(
        self, endpoint: str, *, timeout_seconds: float = 5.0, headers: Mapping[str, str] | None = None
    ) -> None:
        self.endpoint = _http_endpoint(endpoint, "/api/v2/alerts")
        self.timeout_seconds = min(max(timeout_seconds, 0.1), 30.0)
        self.headers = _headers(headers)

    def __call__(self, event: OutputEvent) -> None:
        if event.severity not in {"ERROR", "CRITICAL"}:
            return
        labels = {"alertname": event.event, "severity": event.severity}
        if event.diagnostic_code is not None:
            labels["diagnostic_code"] = event.diagnostic_code
        annotations = {"summary": event.message[:256]}
        client_factory = _http_client_factory()
        with client_factory(timeout=self.timeout_seconds, follow_redirects=False) as client:
            response = client.post(
                self.endpoint,
                headers=self.headers,
                json=[{"labels": labels, "annotations": annotations}],
            )
            response.raise_for_status()


class SnmpV3PacketEncoder(Protocol):
    def __call__(self, event: OutputEvent) -> bytes: ...


@final
class SnmpV3Transport:
    """UDP delivery boundary for deployment-provided SNMPv3 packets."""

    def __init__(self, host: str, port: int, encoder: SnmpV3PacketEncoder, *, timeout_seconds: float = 5.0) -> None:
        self.host = _endpoint_host(host)
        self.port = _bounded_port(port)
        self.encoder = encoder
        self.timeout_seconds = min(max(timeout_seconds, 0.1), 30.0)

    def __call__(self, event: OutputEvent) -> None:
        if event.severity not in {"ERROR", "CRITICAL"}:
            return
        packet = self.encoder(event)
        if not packet or len(packet) > 16_384:
            raise ValueError("SNMPv3 packet is empty or unbounded")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.settimeout(self.timeout_seconds)
            _ = connection.sendto(packet, (self.host, self.port))


@final
class PySnmpV3Transport:
    """SNMPv3 USM trap transport using the optional PySNMP v3arch API."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str,
        auth_protocol: str,
        privacy_protocol: str,
        auth_secret: str,
        privacy_secret: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.host = _endpoint_host(host)
        self.port = _bounded_port(port)
        self.username = _bounded_secret_free(username, "SNMPv3 username")
        self.auth_protocol = auth_protocol
        self.privacy_protocol = privacy_protocol
        self.auth_secret = _bounded_secret(auth_secret, "SNMPv3 auth secret")
        self.privacy_secret = _bounded_secret(privacy_secret, "SNMPv3 privacy secret")
        self.timeout_seconds = min(max(timeout_seconds, 0.1), 30.0)

    def __call__(self, event: OutputEvent) -> None:
        if event.severity not in {"ERROR", "CRITICAL"}:
            return
        asyncio.run(self._send(event))

    async def _send(self, event: OutputEvent) -> None:
        pysnmp_api = cast(_SnmpModule, cast(object, importlib.import_module("pysnmp.hlapi.v3arch.asyncio")))

        auth_protocols = {
            "SHA": pysnmp_api.USM_AUTH_HMAC96_SHA,
            "SHA256": pysnmp_api.USM_AUTH_HMAC192_SHA256,
            "SHA512": pysnmp_api.USM_AUTH_HMAC384_SHA512,
        }
        privacy_protocols = {
            "AES128": pysnmp_api.USM_PRIV_CFB128_AES,
            "AES256": pysnmp_api.USM_PRIV_CFB256_AES,
        }
        auth_protocol = auth_protocols.get(self.auth_protocol)
        privacy_protocol = privacy_protocols.get(self.privacy_protocol)
        if auth_protocol is None or privacy_protocol is None:
            raise ValueError("unsupported SNMPv3 authentication or privacy protocol")
        engine = pysnmp_api.SnmpEngine()
        try:
            error_indication, error_status, _, _ = await pysnmp_api.send_notification(
                engine,
                pysnmp_api.UsmUserData(
                    self.username,
                    authKey=self.auth_secret,
                    privKey=self.privacy_secret,
                    authProtocol=auth_protocol,
                    privProtocol=privacy_protocol,
                ),
                await pysnmp_api.UdpTransportTarget.create(
                    (self.host, self.port),
                    timeout=self.timeout_seconds,
                    retries=0,
                ),
                pysnmp_api.ContextData(),
                "trap",
                pysnmp_api.NotificationType(pysnmp_api.ObjectIdentity("1.3.6.1.6.3.1.1.5.1")).add_varbinds(
                    pysnmp_api.ObjectType(
                        pysnmp_api.ObjectIdentity("1.3.6.1.2.1.1.1.0"),
                        pysnmp_api.OctetString(event.message[:256]),
                    )
                ),
            )
            if error_indication or error_status:
                raise OSError("SNMPv3 notification was rejected")
        finally:
            _ = engine.close_dispatcher()


def build_output_hub(
    transports: Mapping[str, OutputTransport],
    *,
    queue_size: int = 256,
    on_drop: Callable[[str], None] | None = None,
    on_failure: Callable[[str, BaseException], None] | None = None,
) -> OutputHub[OutputEvent]:
    sinks = {
        name: NonBlockingSink(transport, max_queue=queue_size, name=name, on_failure=on_failure)
        for name, transport in transports.items()
    }
    hub = OutputHub(sinks, on_drop=on_drop)
    hub.start()
    return hub


def _endpoint_host(value: str) -> str:
    host = str(value).strip()
    if not host or any(ord(char) < 0x20 for char in host) or len(host) > 255:
        raise ValueError("observability endpoint host is invalid")
    return host


def _validate_event_identity(event: str, message: str, severity: str) -> None:
    if _EVENT.fullmatch(event) is None:
        raise ValueError("observability event name is invalid")
    if not message or len(message) > _MAX_MESSAGE:
        raise ValueError("observability event message is unbounded")
    if severity not in _LEVELS:
        raise ValueError("observability event severity is invalid")


def _validate_attributes(attributes: tuple[tuple[str, str], ...]) -> None:
    if len(attributes) > _MAX_ATTRIBUTES:
        raise ValueError("observability event attributes are unbounded")
    for key, value in attributes:
        if (
            _KEY.fullmatch(key) is None
            or _FORBIDDEN.search(key)
            or not value
            or len(value) > _MAX_VALUE
            or _FORBIDDEN.search(value)
        ):
            raise ValueError("observability event attributes are invalid")


def _validate_context(traceparent: str | None, diagnostic_code: str | None) -> None:
    if traceparent is not None and (not traceparent or len(traceparent) > 128):
        raise ValueError("observability trace context is invalid")
    if diagnostic_code is not None and not re.fullmatch(r"SW[A-Z]+[0-9]{4}", diagnostic_code):
        raise ValueError("observability diagnostic code is invalid")


def _bounded_port(value: int) -> int:
    if not 1 <= int(value) <= 65_535:
        raise ValueError("observability endpoint port is invalid")
    return int(value)


def _bounded_secret_free(value: str, name: str) -> str:
    result = str(value).strip()
    if not result or len(result) > 128 or any(ord(char) < 0x20 for char in result):
        raise ValueError(f"{name} is invalid")
    return result


def _http_client_factory() -> _HttpClientFactory:
    module = importlib.import_module("httpx")
    return cast(_HttpClientFactory, module.__dict__["Client"])


def _bounded_secret(value: str, name: str) -> str:
    result = _bounded_secret_free(value, name)
    if len(result) < 8:
        raise ValueError(f"{name} is too short")
    return result


def _http_endpoint(value: str, suffix: str) -> str:
    endpoint = str(value).strip().rstrip("/")
    if not endpoint.startswith(("http://", "https://")) or len(endpoint) > 512:
        raise ValueError("observability HTTP endpoint is invalid")
    return endpoint if endpoint.endswith(suffix) else endpoint + suffix


def _headers(values: Mapping[str, str] | None) -> dict[str, str]:
    result: dict[str, str] = {"content-type": "application/json"}
    for key, value in (values or {}).items():
        normalized_key = str(key).strip().lower()
        normalized_value = str(value).strip()
        if not _KEY.fullmatch(normalized_key) or not normalized_value or len(normalized_value) > _MAX_VALUE:
            raise ValueError("observability HTTP headers are invalid")
        result[normalized_key] = normalized_value
    return result
