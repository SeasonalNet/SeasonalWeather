"""Initial slixmpp implementation of the normalized NWWS source contract.

All transport-library vocabulary is deliberately confined to this module.  The
controller sees only ``NwwsSource`` and ``NwwsProductEnvelope`` from
``seasonalweather.nwws.source``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import inspect
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import slixmpp

from ..diagnostics.bindings import NWWS_CODES
from .source import (
    NwwsAuthError,
    NwwsDiagnosticSink,
    NwwsInputError,
    NwwsProductEnvelope,
    NwwsProtocolError,
    NwwsSource,
    NwwsTlsError,
    NwwsWireMessage,
    ProductSink,
    SessionCallbacks,
    SessionFactory,
    SessionTransport,
    SourceHealth,
    SourceState,
    _SourceState,
    is_server_banner,
    normalize_nwws_message,
)

log = logging.getLogger("seasonalweather.nwws")

_MAX_STANZA_NODES = 256
_MAX_STANZA_DEPTH = 16
_MAX_STANZA_CHILDREN = 128
_MAX_STANZA_ATTRIBUTES = 32
_MAX_STANZA_METADATA_BYTES = 64 * 1024
_MAX_STANZA_TEXT_BYTES = 1_048_576


class _SlixmppSession(slixmpp.ClientXMPP, SessionTransport):
    """One slixmpp connection bound to the controller event loop."""

    def __init__(
        self,
        jid: str,
        password: str,
        *,
        server: str,
        port: int,
        room_jid: str,
        nick: str,
        muc_confirm_seconds: int,
        callbacks: SessionCallbacks,
    ) -> None:
        super().__init__(jid, password)
        self._configure_transport_security()
        self._server = server
        self._port = int(port)
        self._room_jid = room_jid
        self._nick = nick
        self._muc_confirm_seconds = max(1, min(int(muc_confirm_seconds), 300))
        self._callbacks = callbacks
        self._tls_established = False
        self._tls_failure_reported = False
        self._disconnect_task: asyncio.Task[Any] | None = None
        self.register_plugin("xep_0030")
        self.register_plugin("xep_0199")
        self.register_plugin("xep_0045")
        self.add_event_handler("session_start", self._session_start)
        self.add_event_handler("failed_auth", self._failed_auth)
        self.add_event_handler("presence", self._presence)
        self.add_event_handler("message", self._message)
        self.add_event_handler("disconnected", self._disconnected)
        self.add_event_handler("tls_success", self._tls_success)
        self.add_event_handler("ssl_invalid_chain", self._ssl_invalid_chain)

    def _configure_transport_security(self) -> None:
        """Make NWWS STARTTLS and certificate verification explicit."""
        self.enable_starttls = True
        self.enable_direct_tls = False
        self.enable_plaintext = False
        self.tls_services = set()
        self.starttls_services = {"xmpp-client"}
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_default_verify_paths()
        self.ssl_context = context

        mechanisms = self.plugin["feature_mechanisms"]
        mechanisms.encrypted_plain = True
        mechanisms.unencrypted_plain = False
        mechanisms.unencrypted_cram = False
        mechanisms.unencrypted_scram = False
        mechanisms.unencrypted_digest = False

    def _report_tls_failure(self, error: BaseException) -> None:
        if self._tls_failure_reported:
            return
        self._tls_failure_reported = True
        self._callbacks.failure("tls", error)

    def _tls_success(self, _event: object) -> None:
        socket = self.socket
        if not isinstance(socket, (ssl.SSLSocket, ssl.SSLObject)):
            self._report_tls_failure(NwwsTlsError("NWWS TLS session was not established"))
            self._disconnect_without_wait()
            return
        if self.ssl_context.verify_mode is not ssl.CERT_REQUIRED or not self.ssl_context.check_hostname:
            self._report_tls_failure(NwwsTlsError("NWWS TLS certificate verification is not required"))
            self._disconnect_without_wait()
            return
        self._tls_established = True

    def _ssl_invalid_chain(self, error: BaseException) -> None:
        if self._tls_failure_reported:
            return
        self._report_tls_failure(NwwsTlsError("NWWS TLS certificate verification failed"))
        self._disconnect_without_wait()

    async def _handle_stream_features(self, features: Any) -> bool | None:
        """Reject SASL features until the required STARTTLS upgrade succeeds."""
        offered = features["features"]
        if "mechanisms" in offered and not self._tls_established and "starttls" not in offered:
            self._report_tls_failure(NwwsTlsError("NWWS server did not offer required STARTTLS"))
            self._disconnect_without_wait()
            return True
        return await super()._handle_stream_features(features)

    async def connect(self) -> None:  # type: ignore[override]
        self._tls_established = False
        self._tls_failure_reported = False
        try:
            await slixmpp.ClientXMPP.connect(self, host=self._server, port=self._port)
        except ssl.SSLError as exc:
            self._callbacks.failure("tls", exc)
            raise NwwsTlsError("NWWS trust negotiation failed") from exc
        except (OSError, ConnectionError) as exc:
            self._callbacks.failure("transport", exc)
            raise

    async def disconnect(self) -> None:  # type: ignore[override]
        try:
            if self._disconnect_task is not None:
                await asyncio.wait_for(self._disconnect_task, timeout=1.0)
                return
            result = slixmpp.ClientXMPP.disconnect(self, wait=0.5, ignore_send_queue=True)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The adapter owns the bounded lifecycle; a transport close failure
            # is reported by the source rather than leaking wire state.
            return

    async def _session_start(self, _event: object) -> None:
        if not self._tls_established:
            self._report_tls_failure(NwwsTlsError("NWWS authentication/session started without TLS"))
            self._disconnect_without_wait()
            return
        self._callbacks.authenticated()
        try:
            self.send_presence()
            roster = self.get_roster()
            if inspect.isawaitable(roster):
                await asyncio.wait_for(roster, timeout=self._muc_confirm_seconds)
            plugin: Any = self.plugin["xep_0045"]
            try:
                joined = plugin.join_muc(self._room_jid, self._nick, password=None)
            except TypeError:
                joined = plugin.join_muc(self._room_jid, self._nick, None)
            if inspect.isawaitable(joined):
                await asyncio.wait_for(joined, timeout=self._muc_confirm_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._callbacks.failure("protocol", exc)

    def _failed_auth(self, _event: object) -> None:
        error = NwwsAuthError("NWWS account authentication was rejected")
        self._callbacks.failure("auth", error)
        self._disconnect_without_wait()

    def _presence(self, presence: Any) -> None:
        try:
            sender = presence.get("from")
            sender_text = str(sender or "")
            bare = sender_text.split("/", 1)[0]
            resource = sender_text.split("/", 1)[1] if "/" in sender_text else ""
            if bare.lower() == self._room_jid.lower() and resource == self._nick:
                self._callbacks.joined()
        except Exception as exc:
            self._callbacks.failure("protocol", exc)

    def _message(self, message: Any) -> None:
        try:
            wire = _wire_from_slixmpp_message(message)
            if wire.message_type == "groupchat" and self._room_jid.lower() in (wire.sender or "").lower():
                self._callbacks.joined()
            self._callbacks.message(wire)
        except (NwwsInputError, NwwsProtocolError) as exc:
            self._callbacks.failure("malformed", exc)
        except Exception:
            self._callbacks.failure("malformed", NwwsProtocolError("NWWS stanza could not be normalized"))

    def _disconnected(self, _event: object) -> None:
        self._callbacks.disconnected(None)

    def _disconnect_without_wait(self) -> None:
        try:
            result = slixmpp.ClientXMPP.disconnect(self, wait=0.0, ignore_send_queue=True)
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                self._disconnect_task = task
                task.add_done_callback(_consume_task_exception)
        except Exception:
            return


class SlixmppNwwsSource(_SourceState, NwwsSource):
    """Bounded, controller-owned slixmpp source implementation."""

    def __init__(
        self,
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
        backoff_max_seconds: int | float,
        generation: int,
        generation_provider: Callable[[], int] | None = None,
        diagnostic_sink: NwwsDiagnosticSink | Callable[..., None] | None = None,
        session_factory: SessionFactory | None = None,
        queue_size: int = 200,
        drain_timeout_seconds: float = 5.0,
    ) -> None:
        _SourceState.__init__(
            self,
            generation=generation,
            generation_provider=generation_provider,
            diagnostic_sink=diagnostic_sink,
        )
        self._jid = jid
        self._password = password
        self._server = server
        self._port = int(port)
        self._room_jid = room_jid
        self._nick = nick
        self._stall_seconds = max(0.0, min(float(stall_seconds), 86_400.0))
        self._muc_confirm_seconds = max(1, min(int(muc_confirm_seconds), 300))
        self._start_wait_seconds = max(1.0, min(float(start_wait_seconds), 300.0))
        self._join_wait_seconds = max(1.0, min(float(join_wait_seconds), 600.0))
        self._backoff_max_seconds = max(0.01, min(float(backoff_max_seconds), 600.0))
        self._backoff_initial_seconds = min(1.0, self._backoff_max_seconds)
        self._drain_timeout_seconds = max(0.05, min(float(drain_timeout_seconds), 60.0))
        self._session_factory = session_factory or self._default_session_factory
        self._stop_event = asyncio.Event()
        self._draining = False
        self._queue: asyncio.Queue[NwwsProductEnvelope] = asyncio.Queue(maxsize=max(1, min(int(queue_size), 2_000)))
        self._delivery_task: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._drain_finished = asyncio.Event()
        self._session: SessionTransport | None = None
        self._last_received_monotonic = 0.0
        self._connected_monotonic_value = 0.0
        self._stale_fenced = False
        self._stop_lock = asyncio.Lock()

    async def start(self, sink: ProductSink) -> None:
        if self._delivery_task is not None:
            raise RuntimeError("NWWS source cannot be started twice")
        self._start_task = asyncio.current_task()
        if self._stop_event.is_set():
            self._state = SourceState.STOPPED
            return
        self._state = SourceState.STARTING
        self._delivery_task = asyncio.create_task(self._deliver_loop(sink), name="nwws-source-delivery")
        try:
            backoff = self._backoff_initial_seconds
            while not self._stop_event.is_set() and not self._admission_closed.is_set():
                if not self._is_current_generation():
                    self._fence_stale_generation()
                    break
                self._connection_attempts += 1
                try:
                    await self._run_connection_attempt()
                    if self._stop_event.is_set() or self._admission_closed.is_set() or self._stale_fenced:
                        break
                    self._reconnects += 1
                    self._consecutive_failures += 1
                    self._state = SourceState.DEGRADED
                    self._diagnose(
                        NWWS_CODES["reconnect_degraded"],
                        "NWWS transport disconnected; bounded reconnect is pending.",
                    )
                except asyncio.CancelledError:
                    if not self._stop_event.is_set():
                        raise
                except Exception as exc:
                    if self._stop_event.is_set() or self._admission_closed.is_set() or self._stale_fenced:
                        break
                    self._consecutive_failures += 1
                    self._state = SourceState.DEGRADED
                    self._diagnose(*_failure_diagnostic(exc))
                finally:
                    await self._close_session()
                if self._stop_event.is_set() or self._admission_closed.is_set() or self._stale_fenced:
                    break
                await self._wait_reconnect(backoff)
                backoff = min(self._backoff_max_seconds, max(self._backoff_initial_seconds, backoff * 2.0))
        finally:
            if self._draining and not self._drain_finished.is_set():
                await self._drain_finished.wait()
            await self._close_session()
            await self._cancel_delivery_task()
            if self._start_task is asyncio.current_task():
                self._start_task = None
            self._state = (
                SourceState.STOPPED
                if self._stop_event.is_set() or self._admission_closed.is_set() or self._stale_fenced
                else SourceState.FAILED
            )

    async def drain(self) -> None:
        async with self._stop_lock:
            self._draining = True
            self._admission_closed.set()
            if self._state not in {SourceState.STOPPED, SourceState.FAILED}:
                self._state = SourceState.DRAINING
            try:
                await asyncio.wait_for(self._queue.join(), timeout=self._drain_timeout_seconds)
            except TimeoutError as exc:
                self._diagnose(
                    NWWS_CODES["lifecycle_deadline"],
                    "NWWS source drain exceeded its bounded shutdown window.",
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
            if self._state not in {SourceState.STOPPED, SourceState.FAILED}:
                self._state = SourceState.DRAINING
            self._drain_finished.set()
            await self._close_session()
            await self._cancel_delivery_task()
            task = self._start_task
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._state = SourceState.STOPPED

    def health(self) -> SourceHealth:
        return _SourceState.health(self)

    async def _run_connection_attempt(self) -> None:
        authenticated = asyncio.Event()
        ready = asyncio.Event()
        disconnected = asyncio.Event()
        joined = False
        failure: list[tuple[str, BaseException | None]] = []

        def on_authenticated() -> None:
            authenticated.set()

        def on_joined() -> None:
            nonlocal joined
            joined = True
            ready.set()

        def on_message(message: NwwsWireMessage) -> None:
            self._receive_message(message)

        def on_disconnected(error: BaseException | None) -> None:
            disconnected.set()
            ready.set()

        def on_failure(kind: str, error: BaseException | None) -> None:
            if kind == "malformed":
                self._malformed_dropped += 1
                self._diagnose(
                    NWWS_CODES["malformed_message"],
                    "NWWS inbound stanza was malformed and was dropped.",
                    error,
                )
                return
            failure.append((kind, error))
            authenticated.set()
            ready.set()
            disconnected.set()

        callbacks = SessionCallbacks(on_authenticated, on_joined, on_message, on_disconnected, on_failure)
        self._session = self._session_factory(callbacks)
        self._state = SourceState.CONNECTING
        await asyncio.wait_for(self._session.connect(), timeout=self._start_wait_seconds)
        await asyncio.wait_for(authenticated.wait(), timeout=self._start_wait_seconds)
        if failure:
            raise _session_failure(failure[-1])
        if self._stop_event.is_set() or self._admission_closed.is_set():
            return
        self._state = SourceState.JOINING
        await asyncio.wait_for(ready.wait(), timeout=self._join_wait_seconds)
        if failure:
            raise _session_failure(failure[-1])
        if disconnected.is_set() and not joined:
            raise ConnectionError("NWWS session disconnected before room confirmation")
        self._state = SourceState.CONNECTED
        self._connected_at = dt.datetime.now(dt.UTC)
        self._connected_monotonic_value = asyncio.get_running_loop().time()
        self._consecutive_failures = 0
        while not self._stop_event.is_set() and not self._admission_closed.is_set() and not self._stale_fenced:
            try:
                await asyncio.wait_for(disconnected.wait(), timeout=0.25)
                break
            except TimeoutError:
                pass
            if self._stall_seconds <= 0.0:
                continue
            connected_age = asyncio.get_running_loop().time() - self._last_received_monotonic
            if self._last_received_monotonic == 0.0:
                connected_age = asyncio.get_running_loop().time() - self._connected_monotonic_value
            if connected_age > self._stall_seconds:
                self._state = SourceState.DEGRADED
                self._diagnose(
                    NWWS_CODES["source_silent"],
                    "NWWS source is connected but silent beyond its bounded health threshold.",
                )
                await self._close_session()
                break

    async def _deliver_loop(self, sink: ProductSink) -> None:
        while True:
            if self._stop_event.is_set() and self._queue.empty():
                return
            try:
                envelope = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                if not self._is_current_generation():
                    self._fence_stale_generation()
                    continue
                result = sink.accept(envelope)
                if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                    result = await result
                if result is not False:
                    self._messages_delivered += 1
            finally:
                self._queue.task_done()

    def _receive_message(self, message: NwwsWireMessage) -> None:
        self._messages_received += 1
        if is_server_banner(message):
            return
        try:
            envelope = normalize_nwws_message(message)
        except NwwsInputError as exc:
            self._malformed_dropped += 1
            self._diagnose(NWWS_CODES["malformed_message"], "NWWS inbound data was malformed.", exc)
            return
        self._last_received_monotonic = asyncio.get_running_loop().time()
        self._last_message_at = envelope.received_at
        self._last_message_identity = envelope.identity
        if self._admission_closed.is_set():
            return
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull as exc:
            self._queue_drops += 1
            self._diagnose(NWWS_CODES["reconnect_degraded"], "NWWS product queue reached its bounded capacity.", exc)

    async def _wait_reconnect(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._admission_closed.wait(), timeout=max(0.0, seconds))
        except TimeoutError:
            return

    async def _close_session(self) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        try:
            await asyncio.wait_for(session.disconnect(), timeout=1.5)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            self._diagnose(NWWS_CODES["lifecycle_deadline"], "NWWS transport shutdown exceeded its bound.", exc)
        except Exception as exc:
            self._diagnose(NWWS_CODES["lifecycle_failure"], "NWWS transport shutdown failed safely.", exc)

    async def _cancel_delivery_task(self) -> None:
        task = self._delivery_task
        self._delivery_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    def _fence_stale_generation(self) -> None:
        if self._stale_fenced:
            return
        self._stale_fenced = True
        self._admission_closed.set()
        self._stop_event.set()
        self._state = SourceState.DRAINING
        self._diagnose(
            NWWS_CODES["stale_generation"],
            "NWWS source generation is stale; no further products are authoritative.",
        )

    def _default_session_factory(self, callbacks: SessionCallbacks) -> SessionTransport:
        return _SlixmppSession(
            self._jid,
            self._password,
            server=self._server,
            port=self._port,
            room_jid=self._room_jid,
            nick=self._nick,
            muc_confirm_seconds=self._muc_confirm_seconds,
            callbacks=callbacks,
        )


def _wire_from_slixmpp_message(message: Any) -> NwwsWireMessage:
    message_type = _bounded_message_field(message, "type", 32).strip() or "normal"
    body = _bounded_message_field(message, "body", _MAX_STANZA_TEXT_BYTES)
    sender = _bounded_message_field(message, "from", 256).strip() or None
    stanza_id = (
        _bounded_message_field(message, "id", 128) or _bounded_message_field(message, "stanza_id", 128)
    ).strip() or None
    payload, delayed_delivery_at = _bounded_wire_fields(getattr(message, "xml", None))
    # stanza fields are returned by the bounded helper above
    return NwwsWireMessage(
        body=body,
        payload=payload,
        message_type=message_type,
        sender=sender,
        stanza_id=stanza_id,
        delayed_delivery_at=delayed_delivery_at,
        received_at=dt.datetime.now(dt.UTC),
    )


def _bounded_message_field(message: Any, name: str, limit: int) -> str:
    value = message.get(name)
    if value is None:
        return ""
    if isinstance(value, bytes):
        if len(value) > limit:
            raise NwwsProtocolError(f"NWWS stanza {name} exceeded its bounded size")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise NwwsProtocolError(f"NWWS stanza {name} was not valid UTF-8") from None
    if not isinstance(value, str):
        raise NwwsProtocolError(f"NWWS stanza {name} had an unsupported type")
    if len(value.encode("utf-8")) > limit:
        raise NwwsProtocolError(f"NWWS stanza {name} exceeded its bounded size")
    return value


def _bounded_wire_fields(xml: Any) -> tuple[str | None, dt.datetime | None]:
    if xml is None:
        return None, None
    try:
        return _bounded_stanza_fields(xml)
    except NwwsProtocolError:
        raise
    except (AttributeError, TypeError, ValueError, UnicodeError):
        raise NwwsProtocolError("NWWS stanza metadata was malformed") from None


def _bounded_stanza_fields(xml: Any) -> tuple[str | None, dt.datetime | None]:
    payload: str | None = None
    delayed_delivery_at: dt.datetime | None = None
    metadata_bytes = 0
    for node, _depth in _bounded_stanza_nodes(xml):
        metadata_bytes += _stanza_node_metadata_bytes(node)
        if metadata_bytes > _MAX_STANZA_METADATA_BYTES + _MAX_STANZA_TEXT_BYTES:
            raise NwwsProtocolError("NWWS stanza text exceeded its bounded size")
        payload = _stanza_payload(node, payload)
        delayed_delivery_at = _stanza_delay(node, delayed_delivery_at)
    return payload, delayed_delivery_at


def _bounded_stanza_nodes(xml: Any) -> list[tuple[Any, int]]:
    stack: list[tuple[Any, int]] = [(xml, 0)]
    nodes: list[tuple[Any, int]] = []
    while stack:
        node, depth = stack.pop()
        if len(nodes) >= _MAX_STANZA_NODES:
            raise NwwsProtocolError("NWWS stanza node count exceeded its bounded size")
        if depth > _MAX_STANZA_DEPTH:
            raise NwwsProtocolError("NWWS stanza nesting exceeded its bounded depth")
        children = node
        if len(children) > _MAX_STANZA_CHILDREN:
            raise NwwsProtocolError("NWWS stanza child count exceeded its bounded size")
        nodes.append((node, depth))
        stack.extend((child, depth + 1) for child in reversed(children))
    return nodes


def _stanza_node_metadata_bytes(node: Any) -> int:
    attributes = getattr(node, "attrib", {})
    if not isinstance(attributes, dict) or len(attributes) > _MAX_STANZA_ATTRIBUTES:
        raise NwwsProtocolError("NWWS stanza metadata attributes exceeded its bounded size")
    attribute_bytes = sum(
        len(str(key).encode("utf-8")) + len(str(value).encode("utf-8")) for key, value in attributes.items()
    )
    text = getattr(node, "text", None)
    tail = getattr(node, "tail", None)
    return attribute_bytes + len(str(text or "").encode("utf-8")) + len(str(tail or "").encode("utf-8"))


def _stanza_payload(node: Any, current: str | None) -> str | None:
    tag = str(getattr(node, "tag", "") or "")
    if not (tag.endswith("}x") and "nwws" in tag.lower()):
        return current
    value = (node.text or "").strip()
    if not value:
        return current
    if current is not None:
        raise NwwsProtocolError("NWWS stanza contained duplicate product payload metadata")
    return value


def _stanza_delay(node: Any, current: dt.datetime | None) -> dt.datetime | None:
    tag = str(getattr(node, "tag", "") or "")
    if not tag.endswith("}delay"):
        return current
    stamp = str(node.attrib.get("stamp") or "").strip()
    if not stamp:
        raise NwwsProtocolError("NWWS delayed-delivery metadata had no timestamp")
    if current is not None:
        raise NwwsProtocolError("NWWS stanza contained duplicate delay metadata")
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _session_failure(failure: tuple[str, BaseException | None]) -> BaseException:
    kind, error = failure
    if kind == "auth":
        return NwwsAuthError("NWWS account authentication was rejected")
    if kind == "tls":
        return NwwsTlsError("NWWS trust negotiation failed")
    if kind == "protocol":
        return NwwsProtocolError("NWWS peer protocol exchange failed")
    if isinstance(error, BaseException):
        return error
    return ConnectionError("NWWS transport disconnected")


def _failure_diagnostic(exception: BaseException) -> tuple[str, str, BaseException]:
    if isinstance(exception, NwwsAuthError):
        return NWWS_CODES["auth_failure"], "NWWS authentication failed; credentials were not recorded.", exception
    if isinstance(exception, (NwwsTlsError, ssl.SSLError)):
        return NWWS_CODES["tls_failure"], "NWWS TLS/trust negotiation failed.", exception
    if isinstance(exception, NwwsProtocolError):
        return NWWS_CODES["protocol_failure"], "NWWS protocol exchange failed.", exception
    if isinstance(exception, TimeoutError):
        return NWWS_CODES["lifecycle_deadline"], "NWWS connection exceeded its bounded startup deadline.", exception
    return NWWS_CODES["transport_failure"], "NWWS connection transport failed; reconnect is bounded.", exception


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        try:
            task.exception()
        except BaseException:
            return
