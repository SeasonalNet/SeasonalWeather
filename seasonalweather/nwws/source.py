"""Controller-owned, implementation-neutral NWWS source contracts.

The source package is the only place where an NWWS transport is adapted.  Alert
policy, targeting, deduplication, and publication consume only
``NwwsProductEnvelope`` values from this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from ..diagnostics.bindings import NWWS_CODES

_WMO_RE = re.compile(r"^[A-Z]{4}\d{2}\s+[A-Z]{4}\s+\d{6}(?:\s+[A-Z]{3})?$")
_AWIPS_RE = re.compile(r"^[A-Z0-9]{6,9}$")
_NUMONLY_RE = re.compile(r"^\d{1,6}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_MAX_PRODUCT_BYTES = 1_048_576
_MAX_PROVENANCE_ITEMS = 8


class NwwsInputError(ValueError):
    """The transport supplied an invalid or unsupported normalized message."""


class NwwsAuthError(ConnectionError):
    """The transport rejected the configured account authentication."""


class NwwsTlsError(ConnectionError):
    """The transport could not establish its configured trust/TLS session."""


class NwwsProtocolError(ConnectionError):
    """The transport or peer violated the bounded source protocol."""


class SourceState(StrEnum):
    DISABLED = "disabled"
    STARTING = "starting"
    CONNECTING = "connecting"
    JOINING = "joining"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class NwwsWireMessage:
    """Neutral wire facts produced by an adapter-specific transport."""

    body: str = ""
    payload: str | None = None
    message_type: str = "groupchat"
    sender: str | None = None
    stanza_id: str | None = None
    sequence: str | None = None
    delayed_delivery_at: dt.datetime | None = None
    received_at: dt.datetime | None = None


@dataclass(frozen=True)
class NwwsProductEnvelope:
    """Normalized product facts exposed to controller consumers."""

    source: str
    received_at: dt.datetime
    source_timestamp: dt.datetime | None
    identity: str
    content_hash: str
    raw_text: str
    wmo_heading: str | None
    issuing_office: str | None
    awips_id: str | None
    delayed_delivery_at: dt.datetime | None
    delay_seconds: float | None
    sequence: str | None
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source != "nwws-oi":
            raise ValueError("NWWS envelope source identity is fixed")
        _aware(self.received_at, "received_at")
        if self.source_timestamp is not None:
            _aware(self.source_timestamp, "source_timestamp")
        if self.delayed_delivery_at is not None:
            _aware(self.delayed_delivery_at, "delayed_delivery_at")
        if not _SAFE_ID_RE.fullmatch(self.identity):
            raise ValueError("envelope identity is not bounded")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_hash):
            raise ValueError("envelope content hash is invalid")
        if not self.raw_text or len(self.raw_text.encode("utf-8")) > _MAX_PRODUCT_BYTES:
            raise ValueError("envelope product text is empty or oversized")
        if self.delay_seconds is not None and not 0.0 <= self.delay_seconds <= 31 * 86400:
            raise ValueError("envelope delivery delay is out of bounds")
        if len(self.provenance) > _MAX_PROVENANCE_ITEMS:
            raise ValueError("envelope provenance is oversized")
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "received_at": self.received_at.astimezone(dt.UTC).isoformat(),
            "source_timestamp": _iso(self.source_timestamp),
            "identity": self.identity,
            "content_hash": self.content_hash,
            "raw_text": self.raw_text,
            "wmo_heading": self.wmo_heading,
            "issuing_office": self.issuing_office,
            "awips_id": self.awips_id,
            "delayed_delivery_at": _iso(self.delayed_delivery_at),
            "delay_seconds": self.delay_seconds,
            "sequence": self.sequence,
            "provenance": dict(self.provenance),
        }


class ProductSink(Protocol):
    def accept(self, envelope: NwwsProductEnvelope) -> bool | Awaitable[bool | None] | None:
        """Accept one normalized product envelope at the consumer boundary."""


class NwwsSourceAdmissionFence:
    """Controller-owned, synchronous fence for source-instance admission."""

    def __init__(self) -> None:
        self._active_source: object | None = None
        self._closed = True

    def activate(self, source: object) -> None:
        self._active_source = source
        self._closed = False

    def retire(self, source: object) -> None:
        if self._active_source is source:
            self._active_source = None
            self._closed = True

    def admits(self, source: object) -> bool:
        return not self._closed and self._active_source is source


class NwwsDiagnosticSink(Protocol):
    def emit(
        self,
        code: str,
        *,
        message: str,
        exception: BaseException | None = None,
    ) -> None:
        """Record one already-bounded source diagnostic."""


@dataclass(frozen=True)
class SourceHealth:
    source: str
    state: SourceState
    generation: int
    connection_attempts: int
    reconnects: int
    consecutive_failures: int
    messages_received: int
    messages_delivered: int
    malformed_dropped: int
    queue_drops: int
    last_message_at: dt.datetime | None
    last_message_identity: str | None
    last_diagnostic_code: str | None
    connected_at: dt.datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "state": self.state.value,
            "generation": self.generation,
            "connection_attempts": self.connection_attempts,
            "reconnects": self.reconnects,
            "consecutive_failures": self.consecutive_failures,
            "messages_received": self.messages_received,
            "messages_delivered": self.messages_delivered,
            "malformed_dropped": self.malformed_dropped,
            "queue_drops": self.queue_drops,
            "last_message_at": _iso(self.last_message_at),
            "last_message_identity": self.last_message_identity,
            "last_diagnostic_code": self.last_diagnostic_code,
            "connected_at": _iso(self.connected_at),
        }


@dataclass(frozen=True)
class SessionCallbacks:
    authenticated: Callable[[], None]
    joined: Callable[[], None]
    message: Callable[[NwwsWireMessage], None]
    disconnected: Callable[[BaseException | None], None]
    failure: Callable[[str, BaseException | None], None]


class SessionTransport(Protocol):
    async def connect(self) -> None:
        """Establish the transport and return after the attempt is scheduled."""

    async def disconnect(self) -> None:
        """Close the transport and await its bounded cleanup."""


SessionFactory = Callable[[SessionCallbacks], SessionTransport]
GenerationProvider = Callable[[], int]


class NwwsSource(Protocol):
    async def start(self, sink: ProductSink) -> None:
        """Run the controller-owned source until stop or cancellation."""

    async def drain(self) -> None:
        """Stop admission and finish already accepted normalized products."""

    async def stop(self) -> None:
        """Request permanent shutdown and await owned transport cleanup."""

    def health(self) -> SourceHealth:
        """Return bounded, secret-free source state."""


class _SourceState:
    def __init__(
        self,
        *,
        generation: int,
        generation_provider: GenerationProvider | None,
        diagnostic_sink: NwwsDiagnosticSink | Callable[..., None] | None,
    ) -> None:
        self._generation = generation
        self._generation_provider = generation_provider or (lambda: generation)
        self._diagnostic_sink = diagnostic_sink
        self._admission_closed = asyncio.Event()
        self._state = SourceState.STARTING
        self._connection_attempts = 0
        self._reconnects = 0
        self._consecutive_failures = 0
        self._messages_received = 0
        self._messages_delivered = 0
        self._malformed_dropped = 0
        self._queue_drops = 0
        self._last_message_at: dt.datetime | None = None
        self._last_message_identity: str | None = None
        self._last_diagnostic_code: str | None = None
        self._connected_at: dt.datetime | None = None

    def health(self) -> SourceHealth:
        return SourceHealth(
            source="nwws-oi",
            state=self._state,
            generation=self._generation,
            connection_attempts=self._connection_attempts,
            reconnects=self._reconnects,
            consecutive_failures=self._consecutive_failures,
            messages_received=self._messages_received,
            messages_delivered=self._messages_delivered,
            malformed_dropped=self._malformed_dropped,
            queue_drops=self._queue_drops,
            last_message_at=self._last_message_at,
            last_message_identity=self._last_message_identity,
            last_diagnostic_code=self._last_diagnostic_code,
            connected_at=self._connected_at,
        )

    def _is_current_generation(self) -> bool:
        try:
            return int(self._generation_provider()) == self._generation
        except Exception:
            return False

    def _diagnose(self, code: str, message: str, exception: BaseException | None = None) -> None:
        self._last_diagnostic_code = code
        sink = self._diagnostic_sink
        if sink is None:
            return
        try:
            if callable(sink):
                sink(code, message=message[:256], exception=exception)
            else:
                sink.emit(code, message=message[:256], exception=exception)
        except Exception:
            # Diagnostics are best effort and never become a second source
            # lifecycle authority.
            return


class ReplayNwwsSource(_SourceState):
    """Implementation-neutral fixture/replay source for parity tests."""

    def __init__(
        self,
        messages: Iterable[NwwsWireMessage | Mapping[str, object]],
        *,
        generation: int = 0,
        generation_provider: GenerationProvider | None = None,
        diagnostic_sink: NwwsDiagnosticSink | Callable[..., None] | None = None,
        queue_size: int = 200,
        drain_timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(
            generation=generation,
            generation_provider=generation_provider,
            diagnostic_sink=diagnostic_sink,
        )
        self._messages = tuple(messages)
        self._stop_event = asyncio.Event()
        self._draining = False
        self._queue_size = max(1, min(int(queue_size), 2_000))
        self._drain_timeout_seconds = max(0.05, min(float(drain_timeout_seconds), 60.0))
        self._queue: asyncio.Queue[NwwsProductEnvelope] = asyncio.Queue(maxsize=self._queue_size)
        self._delivery_task: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._drain_finished = asyncio.Event()
        self._stop_lock = asyncio.Lock()

    async def start(self, sink: ProductSink) -> None:
        if self._stop_event.is_set() or self._admission_closed.is_set():
            self._state = SourceState.STOPPED
            return
        self._state = SourceState.CONNECTED
        self._connected_at = dt.datetime.now(dt.UTC)
        self._start_task = asyncio.current_task()
        self._delivery_task = asyncio.create_task(self._deliver_loop(sink), name="nwws-replay-delivery")
        try:
            for item in self._messages:
                if self._stop_event.is_set() or self._admission_closed.is_set():
                    break
                if not self._is_current_generation():
                    self._diagnose(
                        NWWS_CODES["stale_generation"],
                        "NWWS source generation is stale; replay delivery was fenced.",
                    )
                    break
                try:
                    envelope = normalize_nwws_message(item)
                except NwwsInputError as exc:
                    self._malformed_dropped += 1
                    self._diagnose(NWWS_CODES["malformed_message"], "NWWS replay message was malformed.", exc)
                    continue
                self._messages_received += 1
                try:
                    self._queue.put_nowait(envelope)
                except asyncio.QueueFull as exc:
                    self._queue_drops += 1
                    self._diagnose(
                        NWWS_CODES["reconnect_degraded"], "NWWS replay queue reached its bounded capacity.", exc
                    )
                    continue
                self._last_message_at = envelope.received_at
                self._last_message_identity = envelope.identity
            self._state = SourceState.CONNECTED
            await self._stop_event.wait()
        finally:
            if self._draining and not self._drain_finished.is_set():
                await self._drain_finished.wait()
            await self._cancel_delivery_task()
            if self._start_task is asyncio.current_task():
                self._start_task = None
            self._state = SourceState.STOPPED

    async def _deliver_loop(self, sink: ProductSink) -> None:
        while True:
            if self._stop_event.is_set() and self._queue.empty():
                return
            envelope = await self._queue.get()
            try:
                if not self._is_current_generation():
                    self._diagnose(
                        NWWS_CODES["stale_generation"],
                        "NWWS source generation is stale; replay delivery was fenced.",
                    )
                    continue
                result = sink.accept(envelope)
                if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                    result = await result
                if result is not False:
                    self._messages_delivered += 1
            finally:
                self._queue.task_done()

    async def _cancel_delivery_task(self) -> None:
        task = self._delivery_task
        self._delivery_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def drain(self) -> None:
        async with self._stop_lock:
            self._draining = True
            self._admission_closed.set()
            self._stop_event.set()
            self._state = SourceState.DRAINING
            try:
                await asyncio.wait_for(self._queue.join(), timeout=self._drain_timeout_seconds)
            except TimeoutError as exc:
                self._diagnose(
                    NWWS_CODES["lifecycle_deadline"],
                    "NWWS replay drain exceeded its bounded shutdown window.",
                    exc,
                )
            finally:
                await self._cancel_delivery_task()
                self._drain_finished.set()

    async def stop(self) -> None:
        async with self._stop_lock:
            self._draining = True
            self._admission_closed.set()
            self._stop_event.set()
            self._drain_finished.set()
            await self._cancel_delivery_task()
            task = self._start_task
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._state = SourceState.STOPPED


def normalize_nwws_message(message: NwwsWireMessage | Mapping[str, object]) -> NwwsProductEnvelope:
    """Normalize neutral wire facts without importing a transport library."""

    wire = _wire_message(message)
    payload = wire.payload if wire.payload is not None else wire.body
    text = _canonical_text(payload)
    if not text:
        raise NwwsInputError("NWWS message has no product text")
    if len(text.encode("utf-8")) > _MAX_PRODUCT_BYTES:
        raise NwwsInputError("NWWS product exceeds the bounded input size")

    received_at = _aware(wire.received_at or dt.datetime.now(dt.UTC), "received_at")
    delayed_at = wire.delayed_delivery_at
    if delayed_at is not None:
        delayed_at = _aware(delayed_at, "delayed_delivery_at")
    delay_seconds = None
    if delayed_at is not None:
        delay_seconds = max(0.0, (received_at - delayed_at).total_seconds())

    lines = text.splitlines()
    sequence = (wire.sequence or "").strip() or _leading_sequence(lines)
    wmo_heading = next((line.strip() for line in lines[:48] if _WMO_RE.fullmatch(line.strip().upper())), None)
    if wmo_heading:
        wmo_heading = wmo_heading.upper()
    issuing_office = _issuing_office(wmo_heading)
    awips_id = next(
        (
            line.strip().upper()
            for line in lines[:48]
            if _AWIPS_RE.fullmatch(line.strip().upper()) and not line.strip().isdigit()
        ),
        None,
    )
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    identity = _identity(wire.stanza_id, content_hash)
    provenance_values = {
        "message_type": _bounded_value(wire.message_type or "unknown", 32),
    }
    if wire.sender:
        provenance_values["sender"] = _bounded_value(wire.sender, 128)
    if sequence:
        sequence = _bounded_value(sequence, 32)
    return NwwsProductEnvelope(
        source="nwws-oi",
        received_at=received_at,
        source_timestamp=delayed_at,
        identity=identity,
        content_hash=content_hash,
        raw_text=text,
        wmo_heading=wmo_heading,
        issuing_office=issuing_office,
        awips_id=awips_id,
        delayed_delivery_at=delayed_at,
        delay_seconds=delay_seconds,
        sequence=sequence,
        provenance=provenance_values,
    )


def is_server_banner(message: NwwsWireMessage | Mapping[str, object]) -> bool:
    wire = _wire_message(message)
    body = wire.body.strip()
    return wire.message_type.strip().lower() == "normal" and (
        body.startswith("**WARNING**") or "**WARNING**WARNING**" in body[:80]
    )


def build_nwws_source(
    jid: str,
    password: str,
    server: str,
    port: int,
    *,
    room_jid: str,
    nick: str,
    stall_seconds: int,
    muc_confirm_seconds: int,
    start_wait_seconds: int,
    join_wait_seconds: int,
    backoff_max_seconds: int,
    generation: int,
    generation_provider: GenerationProvider | None = None,
    diagnostic_sink: NwwsDiagnosticSink | Callable[..., None] | None = None,
) -> NwwsSource:
    """Build the accepted initial adapter without exposing its wire types."""

    from .slixmpp_adapter import SlixmppNwwsSource

    return SlixmppNwwsSource(
        jid,
        password,
        server,
        port,
        room_jid=room_jid,
        nick=nick,
        stall_seconds=stall_seconds,
        muc_confirm_seconds=muc_confirm_seconds,
        start_wait_seconds=start_wait_seconds,
        join_wait_seconds=join_wait_seconds,
        backoff_max_seconds=backoff_max_seconds,
        generation=generation,
        generation_provider=generation_provider,
        diagnostic_sink=diagnostic_sink,
    )


def _wire_message(message: NwwsWireMessage | Mapping[str, object]) -> NwwsWireMessage:
    if isinstance(message, NwwsWireMessage):
        return message
    if not isinstance(message, Mapping):
        raise NwwsInputError("NWWS message is not a neutral mapping")
    return NwwsWireMessage(
        body=_string_or_empty(message.get("body")),
        payload=_string_or_none(message.get("payload")),
        message_type=_string_or_empty(message.get("message_type")) or "groupchat",
        sender=_string_or_none(message.get("sender")),
        stanza_id=_string_or_none(message.get("stanza_id")),
        sequence=_string_or_none(message.get("sequence")),
        delayed_delivery_at=_datetime_or_none(message.get("delayed_delivery_at")),
        received_at=_datetime_or_none(message.get("received_at")),
    )


def _canonical_text(value: str) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _leading_sequence(lines: list[str]) -> str | None:
    for line in lines[:4]:
        value = line.strip()
        if not value:
            continue
        return value if _NUMONLY_RE.fullmatch(value) else None
    return None


def _issuing_office(wmo_heading: str | None) -> str | None:
    if not wmo_heading:
        return None
    pieces = wmo_heading.split()
    return pieces[1] if len(pieces) > 1 else None


def _identity(stanza_id: str | None, content_hash: str) -> str:
    value = (stanza_id or "").strip()
    return value if _SAFE_ID_RE.fullmatch(value) else f"content-{content_hash[:48]}"


def _bounded_value(value: str, limit: int) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in str(value))[:limit]


def _string_or_empty(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise NwwsInputError("NWWS neutral text field is not a string")
    return value


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise NwwsInputError("NWWS neutral field is not a string")
    return value


def _datetime_or_none(value: object) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NwwsInputError("NWWS timestamp is malformed") from exc
    raise NwwsInputError("NWWS timestamp has an unsupported type")


def _aware(value: dt.datetime, name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise NwwsInputError(f"NWWS {name} is not timezone-aware")
    return value.astimezone(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return value.astimezone(dt.UTC).isoformat() if value is not None else None
