from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from seasonalweather.broadcast.service_runtime import _build_controller_owned_nwws_source, _NwwsQueueSink
from seasonalweather.database import SeasonalDatabase
from seasonalweather.nwws.diagnostics import NwwsRuntimeDiagnosticSink
from seasonalweather.nwws.slixmpp_adapter import (
    SlixmppNwwsSource,
    _SlixmppSession,
    _wire_from_slixmpp_message,
)
from seasonalweather.nwws.source import (
    NwwsAuthError,
    NwwsInputError,
    NwwsProductEnvelope,
    NwwsProtocolError,
    NwwsSourceAdmissionFence,
    NwwsTlsError,
    NwwsWireMessage,
    ReplayNwwsSource,
    SessionCallbacks,
    SourceState,
    normalize_nwws_message,
)
from seasonalweather.runtime_diagnostics.models import CorrelationContext, DiagnosticRole
from seasonalweather.runtime_diagnostics.repository import OccurrenceRepository
from seasonalweather.runtime_diagnostics.service import RuntimeDiagnosticService

NOW = dt.datetime(2026, 8, 12, 12, tzinfo=dt.UTC)
PRODUCT = "000123\nABCD12 KLWX 121200\nKPHI\nTORNADO WARNING\n...PRODUCT TEXT..."


class DiagnosticCapture:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, BaseException | None]] = []
        self.event = asyncio.Event()

    def emit(self, code: str, *, message: str, exception: BaseException | None = None) -> None:
        self.items.append((code, message, exception))
        self.event.set()


class RecordingSink:
    def __init__(self) -> None:
        self.items: list[NwwsProductEnvelope] = []
        self.event = asyncio.Event()

    async def accept(self, envelope: NwwsProductEnvelope) -> None:
        self.items.append(envelope)
        self.event.set()


class FakeSession:
    def __init__(self, callbacks, *, behavior: str = "success") -> None:
        self.callbacks = callbacks
        self.behavior = behavior
        self.disconnected = False
        self.disconnect_calls = 0
        self.connected = asyncio.Event()

    async def connect(self) -> None:
        if self.behavior == "connect-blocked":
            await asyncio.Event().wait()
        if self.behavior == "transport":
            raise OSError("DNS failed for password=synthetic-secret")
        if self.behavior == "auth":
            self.callbacks.failure("auth", NwwsAuthError("password=synthetic-secret"))
            return
        if self.behavior == "protocol":
            self.callbacks.authenticated()
            self.callbacks.failure("protocol", NwwsProtocolError("malformed stanza"))
            return
        self.callbacks.authenticated()
        self.callbacks.joined()
        self.connected.set()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.disconnected = True
        self.callbacks.disconnected(None)

    def emit(self, message: NwwsWireMessage) -> None:
        self.callbacks.message(message)


class FeatureSet:
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def __getitem__(self, key: str) -> set[str]:
        if key != "features":
            raise KeyError(key)
        return self.names


def _slixmpp_session(callbacks: SessionCallbacks | None = None) -> _SlixmppSession:
    if callbacks is None:
        callbacks = SessionCallbacks(lambda: None, lambda: None, lambda _: None, lambda _: None, lambda *_: None)
    return _SlixmppSession(
        "synthetic@example.invalid",
        "synthetic-password",
        server="example.invalid",
        port=5222,
        room_jid="NWWS@conference.example.invalid",
        nick="Synthetic",
        muc_confirm_seconds=1,
        callbacks=callbacks,
    )


def test_slixmpp_transport_requires_verified_starttls_and_disables_fallbacks() -> None:
    session = _slixmpp_session()

    assert session.enable_starttls is True
    assert session.enable_direct_tls is False
    assert session.enable_plaintext is False
    assert session.tls_services == set()
    assert session.starttls_services == {"xmpp-client"}
    assert session.ssl_context.verify_mode is ssl.CERT_REQUIRED
    assert session.ssl_context.check_hostname is True
    assert session.ssl_context.minimum_version is ssl.TLSVersion.TLSv1_2


def test_starttls_unavailable_fails_before_sasl_auth_is_attempted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        failures: list[tuple[str, BaseException | None]] = []
        callbacks = SessionCallbacks(
            lambda: None,
            lambda: None,
            lambda _: None,
            lambda _: None,
            lambda kind, error: failures.append((kind, error)),
        )
        session = _slixmpp_session(callbacks)
        disconnected: list[bool] = []
        monkeypatch.setattr(session, "_disconnect_without_wait", lambda: disconnected.append(True))
        auth_attempted = []
        monkeypatch.setattr(
            session.plugin["feature_mechanisms"],
            "_handle_sasl_auth",
            lambda _: auth_attempted.append(True),
        )

        result = await session._handle_stream_features(FeatureSet({"mechanisms"}))

        assert result is True
        assert not auth_attempted
        assert failures and failures[0][0] == "tls"
        assert isinstance(failures[0][1], NwwsTlsError)
        assert disconnected == [True]

    asyncio.run(exercise())


def test_tls_verification_failure_is_fail_closed_and_does_not_authenticate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failures: list[tuple[str, BaseException | None]] = []
    callbacks = SessionCallbacks(
        lambda: None,
        lambda: None,
        lambda _: None,
        lambda _: None,
        lambda kind, error: failures.append((kind, error)),
    )
    session = _slixmpp_session(callbacks)
    disconnected: list[bool] = []
    monkeypatch.setattr(session, "_disconnect_without_wait", lambda: disconnected.append(True))

    session._ssl_invalid_chain(ssl.SSLCertVerificationError("synthetic verification failure"))
    session._ssl_invalid_chain(ssl.SSLCertVerificationError("duplicate failure"))

    assert len(failures) == 1
    assert failures[0][0] == "tls"
    assert isinstance(failures[0][1], NwwsTlsError)
    assert disconnected == [True]


def test_tls_success_callback_rejects_unverified_handshake_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failures: list[tuple[str, BaseException | None]] = []
    callbacks = SessionCallbacks(
        lambda: None,
        lambda: None,
        lambda _: None,
        lambda _: None,
        lambda kind, error: failures.append((kind, error)),
    )
    session = _slixmpp_session(callbacks)
    disconnected: list[bool] = []
    monkeypatch.setattr(session, "_disconnect_without_wait", lambda: disconnected.append(True))
    monkeypatch.setattr(session, "socket", object())

    session._tls_success(None)

    assert failures and failures[0][0] == "tls"
    assert isinstance(failures[0][1], NwwsTlsError)
    assert disconnected == [True]


def _source(
    factory,
    *,
    diagnostics: DiagnosticCapture | None = None,
    generation: int = 0,
    generation_provider=None,
    stall_seconds: float = 0,
    backoff_max_seconds: float = 0.02,
    drain_timeout_seconds: float = 0.1,
) -> SlixmppNwwsSource:
    return SlixmppNwwsSource(
        "synthetic@example.invalid",
        "synthetic-password",
        "example.invalid",
        5222,
        room_jid="NWWS@conference.example.invalid",
        nick="Synthetic",
        stall_seconds=int(stall_seconds),
        muc_confirm_seconds=1,
        start_wait_seconds=1,
        join_wait_seconds=1,
        backoff_max_seconds=backoff_max_seconds,
        generation=generation,
        generation_provider=generation_provider,
        diagnostic_sink=diagnostics,
        session_factory=factory,
        queue_size=4,
        drain_timeout_seconds=drain_timeout_seconds,
    )


FIXTURE_ROOT = Path(__file__).parent / "support"


def _fixture(name: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8")))


class XmlStanza(dict[str, object]):
    def __init__(self, xml: ET.Element, **values: object) -> None:
        super().__init__(values)
        self.xml = xml


class SlixmppMessageSession(FakeSession):
    def __init__(self, callbacks) -> None:
        super().__init__(callbacks)
        self.slixmpp = _slixmpp_session(callbacks)

    def emit_stanza(self, stanza: object) -> None:
        self.slixmpp._message(stanza)


async def _wait_for_state(source: SlixmppNwwsSource, state: SourceState) -> None:
    async def wait() -> None:
        while source.health().state is not state:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout=1.0)


def test_normalized_envelope_is_transport_neutral_and_preserves_metadata() -> None:
    delayed = NOW - dt.timedelta(seconds=17)
    direct = normalize_nwws_message(
        NwwsWireMessage(
            body=PRODUCT,
            message_type="groupchat",
            sender="NWWS@conference.example.invalid/Synthetic",
            stanza_id="stanza-001",
            delayed_delivery_at=delayed,
            received_at=NOW,
        )
    )
    mapping = normalize_nwws_message(
        {
            "body": PRODUCT,
            "message_type": "groupchat",
            "sender": "NWWS@conference.example.invalid/Synthetic",
            "stanza_id": "stanza-001",
            "delayed_delivery_at": delayed.isoformat(),
            "received_at": NOW.isoformat(),
        }
    )
    assert direct == mapping
    assert direct.identity == "stanza-001"
    assert direct.sequence == "000123"
    assert direct.issuing_office == "KLWX"
    assert direct.delayed_delivery_at == delayed
    assert direct.delay_seconds == 17.0
    assert direct.content_hash


def test_successful_connect_delivers_normalized_products_and_clean_shutdown() -> None:
    async def exercise() -> None:
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks)
            sessions.append(session)
            return session

        source = _source(factory)
        sink = RecordingSink()
        task = asyncio.create_task(source.start(sink), name="test-nwws-source")
        await _wait_for_state(source, SourceState.CONNECTED)
        sessions[0].emit(NwwsWireMessage(body=PRODUCT, stanza_id="product-1", received_at=NOW))
        await asyncio.wait_for(sink.event.wait(), timeout=1.0)
        assert sink.items[0].source == "nwws-oi"
        assert sink.items[0].raw_text == PRODUCT
        await source.drain()
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert source.health().state is SourceState.STOPPED
        assert sessions[0].disconnected
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "nwws-source-delivery"]

    asyncio.run(exercise())


def test_drain_closes_reconnect_admission_after_remote_disconnect() -> None:
    async def exercise() -> None:
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks)
            sessions.append(session)
            return session

        source = _source(factory, backoff_max_seconds=0.2)
        task = asyncio.create_task(source.start(RecordingSink()))
        await _wait_for_state(source, SourceState.CONNECTED)
        await source.drain()
        sessions[0].callbacks.disconnected(None)
        await asyncio.sleep(0.05)
        assert len(sessions) == 1
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "nwws-source-delivery"]

    asyncio.run(exercise())


def test_drain_during_reconnect_backoff_prevents_a_new_attempt() -> None:
    async def exercise() -> None:
        diagnostics = DiagnosticCapture()
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks, behavior="transport")
            sessions.append(session)
            return session

        source = _source(factory, diagnostics=diagnostics, backoff_max_seconds=0.5)
        task = asyncio.create_task(source.start(RecordingSink()))
        await asyncio.wait_for(diagnostics.event.wait(), timeout=1.0)
        await source.drain()
        await asyncio.wait_for(task, timeout=1.0)
        assert len(sessions) == 1
        await source.stop()

    asyncio.run(exercise())


def test_admitted_product_completes_during_drain_but_post_drain_product_is_rejected() -> None:
    class BlockingSink:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.items: list[NwwsProductEnvelope] = []

        async def accept(self, envelope: NwwsProductEnvelope) -> None:
            self.started.set()
            await self.release.wait()
            self.items.append(envelope)

    async def exercise() -> None:
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks)
            sessions.append(session)
            return session

        source = _source(factory)
        sink = BlockingSink()
        task = asyncio.create_task(source.start(sink))
        await _wait_for_state(source, SourceState.CONNECTED)
        sessions[0].emit(NwwsWireMessage(body=PRODUCT, stanza_id="during-drain", received_at=NOW))
        await asyncio.wait_for(sink.started.wait(), timeout=1.0)
        drain_task = asyncio.create_task(source.drain())
        await asyncio.sleep(0.01)
        sink.release.set()
        await asyncio.wait_for(drain_task, timeout=1.0)
        assert [item.identity for item in sink.items] == ["during-drain"]
        sessions[0].emit(NwwsWireMessage(body=PRODUCT, stanza_id="after-drain", received_at=NOW))
        await asyncio.sleep(0.01)
        assert [item.identity for item in sink.items] == ["during-drain"]
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(exercise())


def test_drain_waits_for_admitted_sink_past_connection_poll_interval() -> None:
    class SlowSink:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.items: list[NwwsProductEnvelope] = []

        async def accept(self, envelope: NwwsProductEnvelope) -> None:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            self.items.append(envelope)

    async def exercise() -> None:
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks)
            sessions.append(session)
            return session

        source = _source(factory, drain_timeout_seconds=0.6)
        sink = SlowSink()
        task = asyncio.create_task(source.start(sink))
        await _wait_for_state(source, SourceState.CONNECTED)
        sessions[0].emit(NwwsWireMessage(body=PRODUCT, stanza_id="slow-drain", received_at=NOW))
        await asyncio.wait_for(sink.started.wait(), timeout=1.0)
        drain_task = asyncio.create_task(source.drain())
        await asyncio.sleep(0.3)
        assert not sink.cancelled.is_set()
        assert len(sessions) == 1
        sink.release.set()
        await asyncio.wait_for(drain_task, timeout=1.0)
        assert [item.identity for item in sink.items] == ["slow-drain"]
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "nwws-source-delivery"]

    asyncio.run(exercise())


def test_over_budget_live_drain_cancels_and_reaps_delivery() -> None:
    class NeverCompletes:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def accept(self, _envelope: NwwsProductEnvelope) -> None:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def exercise() -> None:
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks)
            sessions.append(session)
            return session

        diagnostics = DiagnosticCapture()
        source = _source(factory, diagnostics=diagnostics, drain_timeout_seconds=0.05)
        sink = NeverCompletes()
        task = asyncio.create_task(source.start(sink))
        await _wait_for_state(source, SourceState.CONNECTED)
        sessions[0].emit(NwwsWireMessage(body=PRODUCT, stanza_id="timeout-drain", received_at=NOW))
        await asyncio.wait_for(sink.started.wait(), timeout=1.0)
        await asyncio.wait_for(source.drain(), timeout=1.0)
        assert sink.cancelled.is_set()
        assert any(item[0] == "SWNWWS7001" for item in diagnostics.items)
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "nwws-source-delivery"]

    asyncio.run(exercise())


def test_malformed_slixmpp_message_is_dropped_without_reconnect_and_later_valid_delivers() -> None:
    async def exercise() -> None:
        diagnostics = DiagnosticCapture()
        sessions: list[SlixmppMessageSession] = []

        def factory(callbacks):
            session = SlixmppMessageSession(callbacks)
            sessions.append(session)
            return session

        source = _source(factory, diagnostics=diagnostics)
        sink = RecordingSink()
        task = asyncio.create_task(source.start(sink))
        await _wait_for_state(source, SourceState.CONNECTED)
        malformed = [
            {"type": "groupchat", "from": "NWWS@conference.example.invalid/Synthetic", "body": "x" * 1_048_577},
            XmlStanza(
                ET.Element("message"),
                type="groupchat",
                **{"from": "NWWS@conference.example.invalid/Synthetic", "body": ""},
            ),
            object(),
        ]
        deep = ET.Element("message")
        node = deep
        for _ in range(17):
            node = ET.SubElement(node, "child")
        malformed.append(
            XmlStanza(deep, type="groupchat", **{"from": "NWWS@conference.example.invalid/Synthetic", "body": ""})
        )
        for stanza in malformed:
            sessions[0].emit_stanza(stanza)
        await asyncio.wait_for(diagnostics.event.wait(), timeout=1.0)
        await asyncio.sleep(0.02)
        assert len(sessions) == 1
        assert source.health().reconnects == 0
        assert source.health().state is SourceState.CONNECTED
        sessions[0].emit_stanza(
            {
                "type": "groupchat",
                "from": "NWWS@conference.example.invalid/Synthetic",
                "body": PRODUCT,
                "id": "valid-after",
            }
        )
        await asyncio.wait_for(sink.event.wait(), timeout=1.0)
        assert sink.items[0].identity == "valid-after"
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(exercise())


def test_final_admission_fence_wins_after_async_consumer_delay() -> None:
    class DelayedAdmission:
        def __init__(self, inner: _NwwsQueueSink) -> None:
            self.inner = inner
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def accept(self, envelope: NwwsProductEnvelope) -> bool:
            self.started.set()
            await self.release.wait()
            return self.inner.accept(envelope)

    async def exercise() -> None:
        sessions: list[FakeSession] = []
        fence = NwwsSourceAdmissionFence()
        queue: asyncio.Queue[NwwsProductEnvelope] = asyncio.Queue()

        def factory(callbacks):
            session = FakeSession(callbacks)
            sessions.append(session)
            return session

        source = _source(factory)
        fence.activate(source)
        delayed = DelayedAdmission(_NwwsQueueSink(queue, fence, source))
        task = asyncio.create_task(source.start(delayed))
        await _wait_for_state(source, SourceState.CONNECTED)
        sessions[0].emit(NwwsWireMessage(body=PRODUCT, stanza_id="race", received_at=NOW))
        await asyncio.wait_for(delayed.started.wait(), timeout=1.0)
        fence.activate(object())
        delayed.release.set()
        await asyncio.sleep(0.01)
        assert queue.empty()
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(exercise())


def test_failed_authentication_is_diagnostic_and_reconnect_is_bounded() -> None:
    async def exercise() -> None:
        diagnostics = DiagnosticCapture()
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks, behavior="auth")
            sessions.append(session)
            return session

        source = _source(factory, diagnostics=diagnostics)
        task = asyncio.create_task(source.start(RecordingSink()))
        await asyncio.wait_for(diagnostics.event.wait(), timeout=1.0)
        assert diagnostics.items[0][0] == "SWNWWS6001"
        assert "synthetic-secret" not in str(diagnostics.items[0])
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert len(sessions) >= 1
        assert source.health().connection_attempts < 100

    asyncio.run(exercise())


def test_transport_failure_reconnects_without_accumulating_tasks() -> None:
    async def exercise() -> None:
        diagnostics = DiagnosticCapture()
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks, behavior="transport")
            sessions.append(session)
            return session

        source = _source(factory, diagnostics=diagnostics)
        task = asyncio.create_task(source.start(RecordingSink()))
        await asyncio.sleep(0.08)
        assert source.health().connection_attempts >= 2
        assert diagnostics.items
        assert diagnostics.items[0][0] == "SWNWWS3001"
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "nwws-source-delivery"]

    asyncio.run(exercise())


def test_cancellation_during_connect_reaps_owned_delivery_and_transport() -> None:
    async def exercise() -> None:
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks, behavior="connect-blocked")
            sessions.append(session)
            return session

        source = _source(factory)
        task = asyncio.create_task(source.start(RecordingSink()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sessions[0].disconnect_calls == 1
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "nwws-source-delivery"]

    asyncio.run(exercise())


def test_stop_during_connect_cancels_source_task_within_the_shutdown_bound() -> None:
    async def exercise() -> None:
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks, behavior="connect-blocked")
            sessions.append(session)
            return session

        source = _source(factory)
        task = asyncio.create_task(source.start(RecordingSink()))
        await asyncio.sleep(0.01)
        await asyncio.wait_for(source.stop(), timeout=1.0)
        await asyncio.wait_for(task, timeout=1.0)
        assert sessions[0].disconnect_calls >= 1
        assert source.health().state is SourceState.STOPPED

    asyncio.run(exercise())


def test_connected_silent_state_reconnects_with_diagnostic() -> None:
    async def exercise() -> None:
        diagnostics = DiagnosticCapture()
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks)
            sessions.append(session)
            return session

        source = _source(factory, diagnostics=diagnostics, stall_seconds=1)
        source._stall_seconds = 0.02
        task = asyncio.create_task(source.start(RecordingSink()))
        await asyncio.wait_for(diagnostics.event.wait(), timeout=1.0)
        assert diagnostics.items[0][0] == "SWNWWS4002"
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert len(sessions) >= 1

    asyncio.run(exercise())


def test_malformed_data_is_dropped_but_duplicate_policy_remains_controller_owned() -> None:
    async def exercise() -> None:
        diagnostics = DiagnosticCapture()
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks)
            sessions.append(session)
            return session

        source = _source(factory, diagnostics=diagnostics)
        sink = RecordingSink()
        task = asyncio.create_task(source.start(sink))
        await _wait_for_state(source, SourceState.CONNECTED)
        sessions[0].emit(NwwsWireMessage(body="", stanza_id="bad"))
        duplicate = NwwsWireMessage(body=PRODUCT, stanza_id="same", received_at=NOW)
        sessions[0].emit(duplicate)
        sessions[0].emit(duplicate)
        await asyncio.wait_for(sink.event.wait(), timeout=1.0)
        await asyncio.sleep(0.01)
        assert len(sink.items) == 2
        assert source.health().malformed_dropped == 1
        assert sink.items[0].identity == sink.items[1].identity == "same"
        assert "duplicates_dropped" not in source.health().to_dict()
        assert any(item[0] == "SWNWWS1001" for item in diagnostics.items)
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(exercise())


def test_fixture_replay_and_slixmpp_normalization_have_duplicate_parity() -> None:
    valid = _fixture("nwws_duplicate.json")
    slix_envelope = normalize_nwws_message(_wire_from_slixmpp_message(valid))

    async def exercise() -> list[NwwsProductEnvelope]:
        source = ReplayNwwsSource([valid, valid])
        sink = RecordingSink()
        task = asyncio.create_task(source.start(sink))
        await asyncio.wait_for(sink.event.wait(), timeout=1.0)
        while len(sink.items) < 2:
            await asyncio.sleep(0.001)
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)
        return sink.items

    replayed = asyncio.run(exercise())
    assert [item.identity for item in replayed] == [slix_envelope.identity, slix_envelope.identity]
    assert [item.content_hash for item in replayed] == [slix_envelope.content_hash, slix_envelope.content_hash]


def test_support_fixtures_cover_valid_delayed_and_malformed_inputs() -> None:
    valid = normalize_nwws_message(_fixture("nwws_valid_alert.json"))
    assert valid.identity == "fixture-alert-001"
    assert valid.delay_seconds == 17.0
    assert valid.issuing_office == "KLWX"
    with pytest.raises(NwwsInputError):
        normalize_nwws_message(_fixture("nwws_invalid_malformed.json"))


def test_stanza_traversal_is_bounded_and_rejects_malformed_delay_metadata() -> None:
    oversized = {"body": "x" * (1_048_576 + 1)}
    with pytest.raises(NwwsProtocolError, match="bounded"):
        _wire_from_slixmpp_message(oversized)

    too_many_children = ET.Element("message")
    for _ in range(129):
        ET.SubElement(too_many_children, "child")
    with pytest.raises(NwwsProtocolError, match="child count"):
        _wire_from_slixmpp_message(XmlStanza(too_many_children, body=""))

    deep = ET.Element("message")
    current = deep
    for _ in range(17):
        current = ET.SubElement(current, "child")
    with pytest.raises(NwwsProtocolError, match="nesting"):
        _wire_from_slixmpp_message(XmlStanza(deep, body=""))

    malformed_delay = ET.fromstring('<message><delay xmlns="urn:xmpp:delay"/></message>')
    with pytest.raises(NwwsProtocolError, match="timestamp"):
        _wire_from_slixmpp_message(XmlStanza(malformed_delay, body=""))

    normal = ET.fromstring(
        '<message><x xmlns="nwws"><![CDATA[ABCD12 KLWX 121200\nKPHI\nPRODUCT]]></x>'
        '<delay xmlns="urn:xmpp:delay" stamp="2026-08-12T12:00:00Z"/></message>'
    )
    wire = _wire_from_slixmpp_message(XmlStanza(normal, body=""))
    assert normalize_nwws_message(wire).raw_text.startswith("ABCD12 KLWX")


def test_controller_admission_fence_rejects_replaced_source_synchronously() -> None:
    queue: asyncio.Queue[NwwsProductEnvelope] = asyncio.Queue()
    fence = NwwsSourceAdmissionFence()
    old_source = object()
    new_source = object()
    envelope = normalize_nwws_message({"body": PRODUCT, "stanza_id": "fenced", "received_at": NOW.isoformat()})
    fence.activate(old_source)
    sink = _NwwsQueueSink(queue, fence, old_source)
    fence.activate(new_source)
    assert sink.accept(envelope) is False
    assert queue.empty()


def test_unrelated_configuration_generation_does_not_fence_unchanged_source() -> None:
    resilience = SimpleNamespace(
        stall_seconds=30,
        muc_confirm_seconds=5,
        start_wait_seconds=5,
        join_wait_seconds=5,
        backoff_max_seconds=30,
    )
    owner = SimpleNamespace(
        jid="synthetic@example.invalid",
        password="synthetic-password",
        nwws_server="example.invalid",
        nwws_port=5222,
        nwws_diagnostic_sink=None,
        cfg=SimpleNamespace(
            nwws=SimpleNamespace(
                room="NWWS@conference.example.invalid",
                nick="Synthetic",
                resiliency=resilience,
            )
        ),
    )
    with patch("seasonalweather.broadcast.service_runtime.build_nwws_source") as builder:
        _build_controller_owned_nwws_source(owner)
    assert builder.call_args.kwargs["generation"] == 0
    assert "generation_provider" not in builder.call_args.kwargs


def test_stale_generation_fences_source_replacement_before_delivery() -> None:
    async def exercise() -> None:
        diagnostics = DiagnosticCapture()
        current = [0]
        sessions: list[FakeSession] = []

        def factory(callbacks):
            session = FakeSession(callbacks)
            sessions.append(session)
            return session

        source = _source(factory, diagnostics=diagnostics, generation_provider=lambda: current[0])
        sink = RecordingSink()
        task = asyncio.create_task(source.start(sink))
        await _wait_for_state(source, SourceState.CONNECTED)
        current[0] = 1
        sessions[0].emit(NwwsWireMessage(body=PRODUCT, stanza_id="stale", received_at=NOW))
        await asyncio.wait_for(diagnostics.event.wait(), timeout=1.0)
        await asyncio.sleep(0.01)
        assert not sink.items
        assert diagnostics.items[-1][0] == "SWNWWS8001"
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(exercise())


def test_replay_source_uses_same_consumer_envelope_contract() -> None:
    async def exercise() -> None:
        source = ReplayNwwsSource(
            [
                {"body": PRODUCT, "stanza_id": "replay-1", "received_at": NOW.isoformat()},
                {"body": "", "stanza_id": "invalid"},
            ]
        )
        sink = RecordingSink()
        task = asyncio.create_task(source.start(sink))
        await asyncio.wait_for(sink.event.wait(), timeout=1.0)
        await source.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert len(sink.items) == 1
        assert sink.items[0].identity == "replay-1"
        assert source.health().malformed_dropped == 1

    asyncio.run(exercise())


def test_replay_drain_waits_for_admitted_sink_and_preserves_completion() -> None:
    class SlowSink:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.items: list[NwwsProductEnvelope] = []

        async def accept(self, envelope: NwwsProductEnvelope) -> None:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            self.items.append(envelope)

    async def exercise() -> None:
        source = ReplayNwwsSource(
            [{"body": PRODUCT, "stanza_id": "replay-drain", "received_at": NOW.isoformat()}],
            drain_timeout_seconds=0.6,
        )
        sink = SlowSink()
        task = asyncio.create_task(source.start(sink))
        await asyncio.wait_for(sink.started.wait(), timeout=1.0)
        drain_task = asyncio.create_task(source.drain())
        await asyncio.sleep(0.1)
        assert not sink.cancelled.is_set()
        sink.release.set()
        await asyncio.wait_for(drain_task, timeout=1.0)
        assert [item.identity for item in sink.items] == ["replay-drain"]
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(exercise())


def test_replay_over_budget_drain_cancels_and_reaps_delivery() -> None:
    class NeverCompletes:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def accept(self, _envelope: NwwsProductEnvelope) -> None:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def exercise() -> None:
        diagnostics = DiagnosticCapture()
        source = ReplayNwwsSource(
            [{"body": PRODUCT, "stanza_id": "replay-timeout", "received_at": NOW.isoformat()}],
            diagnostic_sink=diagnostics,
            drain_timeout_seconds=0.05,
        )
        sink = NeverCompletes()
        task = asyncio.create_task(source.start(sink))
        await asyncio.wait_for(sink.started.wait(), timeout=1.0)
        await asyncio.wait_for(source.drain(), timeout=1.0)
        assert sink.cancelled.is_set()
        assert any(item[0] == "SWNWWS7001" for item in diagnostics.items)
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(exercise())


def test_runtime_diagnostic_evidence_redacts_auth_exception(tmp_path: Path) -> None:
    database = SeasonalDatabase(path=str(tmp_path / "diagnostics.sqlite3"))
    database.bootstrap()
    service = RuntimeDiagnosticService(OccurrenceRepository(database))
    service.initialize()
    context = CorrelationContext(
        role=DiagnosticRole.CONTROLLER,
        instance_id="controller-test-1",
        component="controller",
    )
    sink = NwwsRuntimeDiagnosticSink(service, context, generation_provider=lambda: 4)
    sink.emit(
        "SWNWWS6001",
        message="NWWS authentication failed; credentials were not recorded.",
        exception=RuntimeError("password=live-secret token=live-token"),
    )
    record = service.repository.recent()[0]
    encoded = str(record.latest_instance)
    assert "live-secret" not in encoded
    assert "live-token" not in encoded
    assert "[REDACTED]" in encoded
