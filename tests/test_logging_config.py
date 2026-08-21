from __future__ import annotations

import asyncio
import io
import logging
import sys
import xml.etree.ElementTree as ET
from contextlib import suppress
from types import SimpleNamespace
from typing import cast

from slixmpp.xmlstream.xmlstream import XMLStream

from seasonalweather.config import AppConfig, load_config
from seasonalweather.logging_config import setup_logging
from seasonalweather.nwws import smoke_test
from seasonalweather.nwws.source import normalize_nwws_message


def test_config_loads_runtime_log_color(monkeypatch) -> None:
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "test-source")
    cfg = load_config("config/config.yaml")

    assert cfg.logs.runtime.color == "never"


def test_log_color_never_avoids_ansi(monkeypatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    cfg = SimpleNamespace(
        logs=SimpleNamespace(
            runtime=SimpleNamespace(
                level="INFO",
                color="never",
                httpx_level="WARNING",
                httpcore_level="WARNING",
                uvicorn_access_level="WARNING",
                uvicorn_error_level="INFO",
                asyncio_level="WARNING",
                slixmpp_level="WARNING",
                slixmpp_xmlstream_level="WARNING",
                logger_levels={},
                cap_poll_summary=True,
                ipaws_poll_summary=True,
                conductor_cycle_push=True,
                conductor_alert_push=True,
                conductor_live_time_push=True,
                segment_refresher_synth=True,
                segment_refresher_alert_lifecycle=True,
            )
        )
    )

    setup_logging(cast(AppConfig, cast(object, cfg)))
    logging.getLogger("seasonalweather.test").info("plain message")

    assert "\x1b[" not in stream.getvalue()


def test_log_color_always_adds_ansi(monkeypatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    cfg = SimpleNamespace(
        logs=SimpleNamespace(
            runtime=SimpleNamespace(
                level="INFO",
                color="always",
                httpx_level="WARNING",
                httpcore_level="WARNING",
                uvicorn_access_level="WARNING",
                uvicorn_error_level="INFO",
                asyncio_level="WARNING",
                slixmpp_level="WARNING",
                slixmpp_xmlstream_level="WARNING",
                logger_levels={},
                cap_poll_summary=True,
                ipaws_poll_summary=True,
                conductor_cycle_push=True,
                conductor_alert_push=True,
                conductor_live_time_push=True,
                segment_refresher_synth=True,
                segment_refresher_alert_lifecycle=True,
            )
        )
    )

    setup_logging(cast(AppConfig, cast(object, cfg)))
    logging.getLogger("seasonalweather.test").warning("colored message")

    assert "\x1b[" in stream.getvalue()


def test_secret_values_are_redacted_from_application_logs(monkeypatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    cfg = SimpleNamespace(
        logs=SimpleNamespace(
            runtime=SimpleNamespace(
                level="INFO",
                color="never",
                httpx_level="WARNING",
                httpcore_level="WARNING",
                uvicorn_access_level="WARNING",
                uvicorn_error_level="INFO",
                asyncio_level="WARNING",
                slixmpp_level="WARNING",
                slixmpp_xmlstream_level="WARNING",
                logger_levels={},
                cap_poll_summary=True,
                ipaws_poll_summary=True,
                conductor_cycle_push=True,
                conductor_alert_push=True,
                conductor_live_time_push=True,
                segment_refresher_synth=True,
                segment_refresher_alert_lifecycle=True,
            )
        )
    )

    setup_logging(cast(AppConfig, cast(object, cfg)))
    logging.getLogger("seasonalweather.security-test").error(
        "password=log-secret token=log-token authorization=Bearer log-bearer"
    )

    output = stream.getvalue()
    assert "log-secret" not in output
    assert "log-token" not in output
    assert "log-bearer" not in output
    assert output.count("[REDACTED]") == 3


def test_slixmpp_payload_records_are_contained_at_output_boundary(monkeypatch) -> None:
    output_stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output_stream)
    cfg = SimpleNamespace(
        logs=SimpleNamespace(
            runtime=SimpleNamespace(
                level="DEBUG",
                color="never",
                httpx_level="WARNING",
                httpcore_level="WARNING",
                uvicorn_access_level="WARNING",
                uvicorn_error_level="INFO",
                asyncio_level="WARNING",
                slixmpp_level="DEBUG",
                slixmpp_xmlstream_level="DEBUG",
                logger_levels={},
                cap_poll_summary=True,
                ipaws_poll_summary=True,
                conductor_cycle_push=True,
                conductor_alert_push=True,
                conductor_live_time_push=True,
                segment_refresher_synth=True,
                segment_refresher_alert_lifecycle=True,
            )
        )
    )

    setup_logging(cast(AppConfig, cast(object, cfg)))
    slix = logging.getLogger("slixmpp")
    xmlstream = logging.getLogger("slixmpp.xmlstream")
    slix.error("raw malformed-data ERROR body=secret-product auth=password=secret-pass")
    xmlstream.debug("RECV: <message from='secret@example.invalid'>secret-product</message>")
    slix.debug("SEND: <auth username='secret-user'>secret-password</auth>")

    xmpp_stream = XMLStream()
    xmpp_stream.init_parser()
    xmpp_stream.start_stream_handler = lambda _root: None
    xmpp_stream.data_received(
        b"<stream:stream xmlns:stream='http://etherx.jabber.org/streams' from='secret@example.invalid'>"
    )
    with suppress(Exception):
        xmpp_stream.send_raw("<auth username='secret-user'>secret-password</auth>")
    xmpp_stream._build_stanza = lambda _xml: (_ for _ in ()).throw(
        ValueError("stanza build failed body=secret-product")
    )
    xmpp_stream._spawn_event(ET.Element("message"))
    xmpp_stream.init_parser()
    xmpp_stream.send = lambda _stanza: None
    xmpp_stream.disconnect = lambda: None
    xmpp_stream.data_received(b"<message body='secret-product'")

    ordinary = logging.getLogger("seasonalweather.test")
    ordinary.info("ordinary SeasonalWeather record remains visible")

    output = output_stream.getvalue()
    assert "ordinary SeasonalWeather record remains visible" in output
    assert "raw malformed-data" not in output
    assert "RECV:" not in output
    assert "SEND:" not in output
    assert "secret-product" not in output
    assert "secret-pass" not in output
    assert "secret-password" not in output


def test_standalone_smoke_logging_uses_containment_when_levels_are_lowered(monkeypatch) -> None:
    output_stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output_stream)

    def load_smoke_env(_path):
        slix = logging.getLogger("slixmpp")
        xmlstream = logging.getLogger("slixmpp.xmlstream")
        slix.setLevel(logging.DEBUG)
        xmlstream.setLevel(logging.DEBUG)
        slix.error("smoke raw ERROR SMOKE_RAW_SENTINEL")
        xmlstream.debug("RECV: <message>SMOKE_RAW_SENTINEL</message>")
        xmlstream.debug("SEND: <auth>SMOKE_RAW_SENTINEL</auth>")
        logging.getLogger("seasonalweather.nwws_smoke").info("smoke metadata remains visible")
        return {}

    monkeypatch.setattr(smoke_test, "load_env_file", load_smoke_env)

    assert smoke_test.main() == 2
    output = output_stream.getvalue()
    assert "smoke metadata remains visible" in output
    assert "SMOKE_RAW_SENTINEL" not in output


def test_smoke_sink_logs_metadata_without_raw_product_text(caplog) -> None:
    envelope = normalize_nwws_message(
        {
            "body": "SMOKE_RAW_PRODUCT_SENTINEL\n000123\nABCD12 KLWX 121200\nKPHI\n",
            "stanza_id": "smoke-metadata",
        }
    )
    sink = smoke_test._SmokeSink()

    with caplog.at_level(logging.INFO, logger="seasonalweather.nwws_smoke"):
        asyncio.run(sink.accept(envelope))

    assert "SMOKE_RAW_PRODUCT_SENTINEL" not in caplog.text
    assert "smoke-metadata" in caplog.text
    assert envelope.content_hash in caplog.text
