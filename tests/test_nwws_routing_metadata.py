import datetime as dt
from types import SimpleNamespace

from seasonalweather.alerts.product import ParsedProduct
from seasonalweather.broadcast.nwws_runtime import _diagnose_ugc_vtec_expiry_disagreement
from seasonalweather.diagnostics.bindings import NWWS_CODES


UTC = dt.UTC


class _DiagnosticSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def emit(self, code: str, **kwargs: object) -> None:
        self.calls.append((code, kwargs))


def _runtime(sink: _DiagnosticSink) -> SimpleNamespace:
    return SimpleNamespace(nwws_diagnostic_sink=sink)


def _product() -> ParsedProduct:
    return ParsedProduct(
        product_type="SVS",
        wfo="KLWX",
        awips_id="SVSLWX",
        vtec=None,
        raw_text="",
    )


def test_ugc_only_product_is_not_a_ugc_vtec_disagreement() -> None:
    sink = _DiagnosticSink()
    expires = dt.datetime(2026, 8, 28, 22, tzinfo=UTC)

    _diagnose_ugc_vtec_expiry_disagreement(_runtime(sink), _product(), expires, None)

    assert sink.calls == []


def test_matching_ugc_and_vtec_expiry_is_not_reported() -> None:
    sink = _DiagnosticSink()
    expires = dt.datetime(2026, 8, 28, 22, tzinfo=UTC)

    _diagnose_ugc_vtec_expiry_disagreement(_runtime(sink), _product(), expires, expires + dt.timedelta(minutes=1))

    assert sink.calls == []


def test_materially_different_ugc_and_vtec_expiry_is_reported() -> None:
    sink = _DiagnosticSink()
    ugc_expires = dt.datetime(2026, 8, 28, 22, tzinfo=UTC)
    vtec_expires = ugc_expires + dt.timedelta(minutes=2)

    _diagnose_ugc_vtec_expiry_disagreement(_runtime(sink), _product(), ugc_expires, vtec_expires)

    assert len(sink.calls) == 1
    assert sink.calls[0][0] == NWWS_CODES["routing_metadata_disagreement"]
    assert "ugc=2026-08-28T22:00:00+00:00" in str(sink.calls[0][1]["message"])
    assert "vtec=2026-08-28T22:02:00+00:00" in str(sink.calls[0][1]["message"])
