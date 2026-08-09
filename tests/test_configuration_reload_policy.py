from __future__ import annotations

from pathlib import Path

import pytest

from seasonalweather.configuration import compile_path
from seasonalweather.configuration.paths import ConfigPath
from seasonalweather.configuration_reload.candidate_store import CandidateIntegrityError, CandidateStore
from seasonalweather.configuration_reload.diff import build_reload_diff
from seasonalweather.configuration_reload.models import ReloadDisposition
from seasonalweather.configuration_reload.policy import (
    ALL_POLICY_PATTERNS,
    AUTHORITATIVE_POLICY_PATTERNS,
    DECLARED_POLICY_PATTERNS,
    RULES,
    SCHEMA_LEAF_PATTERNS,
    UnclassifiedPathError,
    classify_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config/config.yaml"
ENVIRONMENT = {
    "ICECAST_SOURCE_PASSWORD": "synthetic-icecast-password",
    "SEASONAL_API_TOKEN": "synthetic-test-api-token",
}


def _candidate(tmp_path: Path, replacement: tuple[str, str]) -> Path:
    path = tmp_path / "candidate.yaml"
    text = EXAMPLE.read_text(encoding="utf-8").replace(*replacement, 1)
    path.write_text(text, encoding="utf-8")
    return path


def test_reload_policy_exhaustively_classifies_current_schema_and_unknown_fails_closed() -> None:
    assert DECLARED_POLICY_PATTERNS == AUTHORITATIVE_POLICY_PATTERNS
    assert set(SCHEMA_LEAF_PATTERNS).issubset(ALL_POLICY_PATTERNS)
    assert len(RULES) == len(ALL_POLICY_PATTERNS)
    assert {rule.pattern for rule in RULES} == set(ALL_POLICY_PATTERNS)
    assert classify_path(ConfigPath(("dedupe", "ttl_seconds"))).disposition is ReloadDisposition.LIVE
    assert classify_path(ConfigPath(("tts", "voice"))).disposition is ReloadDisposition.QUIESCENT
    assert classify_path(ConfigPath(("database", "path"))).disposition is ReloadDisposition.RESTART_REQUIRED
    with pytest.raises(UnclassifiedPathError):
        classify_path(ConfigPath(("future", "unclassified")))
    with pytest.raises(UnclassifiedPathError):
        classify_path(ConfigPath(("dedupe", "future_field")))
    with pytest.raises(UnclassifiedPathError):
        classify_path(ConfigPath(("tts", "future_field")))


def test_candidate_capture_is_immutable_and_hash_verified(tmp_path: Path) -> None:
    source = _candidate(tmp_path, ("ttl_seconds: 900", "ttl_seconds: 901"))
    store = CandidateStore(tmp_path / "candidates", environ=ENVIRONMENT, identity_key=b"a" * 32)
    record, _compiled = store.capture(source)
    captured = store.read_bytes(record)

    source.write_text("changed after capture\n", encoding="utf-8")

    assert store.read_bytes(record) == captured
    assert store.verify(record) == captured
    artifact = store.root / record.reference / "source.bin"
    artifact.chmod(0o600)
    artifact.write_bytes(b"tampered")
    with pytest.raises(CandidateIntegrityError):
        store.verify(record)


def test_diff_is_deterministic_classified_and_source_only_noop(tmp_path: Path) -> None:
    active = compile_path(EXAMPLE, environ=ENVIRONMENT)
    candidate_path = _candidate(
        tmp_path,
        ("dedupe:\n  ttl_seconds: 900", "dedupe:\n  ttl_seconds: 901"),
    )
    candidate = compile_path(candidate_path, environ=ENVIRONMENT)
    kwargs = {
        "active_generation": 7,
        "active_identity_sha256": "a" * 64,
        "candidate_identity_sha256": "b" * 64,
        "report_sha256": "c" * 64,
    }
    first = build_reload_diff(active, candidate, **kwargs)
    second = build_reload_diff(active, candidate, **kwargs)

    assert first == second
    assert first.digest == second.digest
    assert first.disposition is ReloadDisposition.LIVE
    assert [entry.path.to_pointer() for entry in first.entries] == ["/dedupe/ttl_seconds"]
    noop = build_reload_diff(active, active, **kwargs)
    assert not noop.effective_change
    assert noop.source_only_change


def test_restart_required_mixed_candidate_is_wholly_report_only(tmp_path: Path) -> None:
    active = compile_path(EXAMPLE, environ=ENVIRONMENT)
    text = EXAMPLE.read_text(encoding="utf-8")
    text = text.replace(
        "dedupe:\n  ttl_seconds: 900",
        "dedupe:\n  ttl_seconds: 901",
        1,
    )
    text = text.replace(
        'path: "/var/lib/seasonalweather/seasonalweather.sqlite3"',
        'path: "/tmp/replacement.sqlite3"',
        1,
    )
    candidate_path = tmp_path / "mixed.yaml"
    candidate_path.write_text(text, encoding="utf-8")
    candidate = compile_path(candidate_path, environ=ENVIRONMENT)
    diff = build_reload_diff(
        active,
        candidate,
        active_generation=0,
        active_identity_sha256="a" * 64,
        candidate_identity_sha256="b" * 64,
        report_sha256="c" * 64,
    )

    assert diff.disposition is ReloadDisposition.RESTART_REQUIRED
    assert diff.grouped_paths()["live"]
    assert diff.grouped_paths()["restart_required"]


def test_secret_change_is_visible_without_rendering_old_or_new_secret(tmp_path: Path) -> None:
    old_environment = ENVIRONMENT | {"ICECAST_SOURCE_PASSWORD": "old-private-sentinel"}
    new_environment = ENVIRONMENT | {"ICECAST_SOURCE_PASSWORD": "new-private-sentinel"}
    active = compile_path(EXAMPLE, environ=old_environment)
    candidate = compile_path(EXAMPLE, environ=new_environment)
    diff = build_reload_diff(
        active,
        candidate,
        active_generation=0,
        active_identity_sha256="a" * 64,
        candidate_identity_sha256="b" * 64,
        report_sha256="c" * 64,
        active_environment_inputs=(
            {
                "variable": "ICECAST_SOURCE_PASSWORD",
                "present": True,
                "opaque_change_identity": "hmac-sha256:old",
            },
        ),
        candidate_environment_inputs=(
            {
                "variable": "ICECAST_SOURCE_PASSWORD",
                "present": True,
                "opaque_change_identity": "hmac-sha256:new",
            },
        ),
    )
    rendered = str(diff.to_dict())

    assert any(entry.secret for entry in diff.entries)
    assert "old-private-sentinel" not in rendered
    assert "new-private-sentinel" not in rendered
    assert "<redacted:presence-changed>" in rendered


def test_rendered_endpoint_removes_userinfo_query_and_fragment(tmp_path: Path) -> None:
    active = compile_path(EXAMPLE, environ=ENVIRONMENT)
    candidate = compile_path(
        _candidate(
            tmp_path,
            (
                '  url: ""                      # leave empty to use default NWS alerts endpoint',
                '  url: "https://endpoint-user:endpoint-pass@example.invalid/path?query-token=sentinel#fragment-sentinel"',
            ),
        ),
        environ=ENVIRONMENT,
    )
    diff = build_reload_diff(
        active,
        candidate,
        active_generation=0,
        active_identity_sha256="a" * 64,
        candidate_identity_sha256="b" * 64,
        report_sha256="c" * 64,
    )
    rendered = str(diff.to_dict())

    assert "https://example.invalid/path" in rendered
    for sentinel in ("endpoint-user", "endpoint-pass", "query-token", "fragment-sentinel"):
        assert sentinel not in rendered
