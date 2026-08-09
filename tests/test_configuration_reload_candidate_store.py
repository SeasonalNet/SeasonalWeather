from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from seasonalweather.configuration_reload.candidate_store import CandidateIntegrityError, CandidateStore

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config/config.yaml"
ENVIRONMENT = {
    "ICECAST_SOURCE_PASSWORD": "candidate-store-secret",
    "SEASONAL_API_TOKEN": "candidate-store-api-token",
}


def test_candidate_capture_is_immutable_verified_and_secret_safe(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    source.write_bytes(EXAMPLE.read_bytes())
    store = CandidateStore(tmp_path / "store", environ=ENVIRONMENT, identity_key=b"k" * 32)
    record, _compiled = store.capture(source)
    captured = store.read_bytes(record)

    source.write_text("changed: true\n", encoding="utf-8")
    assert store.verify(record) == captured
    metadata = (tmp_path / "store" / record.reference / "metadata.json").read_text(encoding="utf-8")
    assert "candidate-store-secret" not in metadata
    assert "candidate-store-api-token" not in metadata
    assert "hmac-sha256:" in metadata


def test_candidate_tamper_and_symlink_input_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    source.write_bytes(EXAMPLE.read_bytes())
    store = CandidateStore(tmp_path / "store", environ=ENVIRONMENT, identity_key=b"k" * 32)
    record, _compiled = store.capture(source)
    stored_source = tmp_path / "store" / record.reference / "source.bin"
    stored_source.write_bytes(b"tampered")
    with pytest.raises(CandidateIntegrityError):
        store.verify(record)

    link = tmp_path / "link.yaml"
    link.symlink_to(source)
    with pytest.raises(Exception):
        store.capture(link)


def test_candidate_retention_never_removes_protected_evidence(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    source.write_bytes(EXAMPLE.read_bytes())
    captured_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    store = CandidateStore(
        tmp_path / "store",
        environ=ENVIRONMENT,
        identity_key=b"k" * 32,
        clock=lambda: captured_at,
    )
    record, _compiled = store.capture(source)

    boundary = captured_at + dt.timedelta(days=1)
    assert store.cleanup(retain_after=boundary, protected_references=frozenset({record.reference})) == ()
    assert store.load(record.reference) == record
    assert store.cleanup(retain_after=boundary, protected_references=frozenset()) == (record.reference,)
