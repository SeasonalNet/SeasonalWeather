from __future__ import annotations

import datetime as dt

from seasonalweather.configuration.paths import ConfigPath
from seasonalweather.configuration_reload.models import (
    CandidateRecord,
    ChangeKind,
    DiffEntry,
    ReloadDiff,
    ReloadDisposition,
    ReloadOutcome,
    ReloadPhase,
    ReloadResult,
    SafePointSnapshot,
    WarningAcknowledgment,
)

NOW = dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC)


def test_public_reload_contracts_defensively_freeze_nested_caller_inputs() -> None:
    source_item = {"source": "candidate.yaml", "sha256": "1" * 64, "byte_length": 4, "bytes_available": True}
    origin_item = {"path": "/dedupe/ttl_seconds", "kind": "default", "declaration_id": "schema.default"}
    environment_item = {"variable": "SEASONAL_API_TOKEN", "present": False}
    candidate = CandidateRecord(
        reference="candidate_" + ("a" * 40),
        source_name="candidate.yaml",
        source_sha256="1" * 64,
        candidate_sha256="2" * 64,
        byte_length=4,
        candidate_identity_sha256="3" * 64,
        config_schema_version=1,
        source_manifest=[source_item],
        origin_manifest=[origin_item],
        environment_inputs=[environment_item],
        captured_at=NOW,
    )
    old_value = {"nested": ["before"]}
    location = {"start": {"line": 1, "column": 1}}
    entry = DiffEntry(
        path=ConfigPath(("dedupe", "ttl_seconds")),
        classification=ReloadDisposition.LIVE,
        policy_id="reload.v1.live.dedupe.ttl_seconds",
        kind=ChangeKind.REPLACE,
        secret=False,
        old=old_value,
        new=901,
        source_location=location,
    )
    diff = ReloadDiff(
        active_generation=0,
        active_identity_sha256="4" * 64,
        candidate_identity_sha256="3" * 64,
        report_sha256="5" * 64,
        entries=[entry],
    )
    changed_paths = {"live": ["/dedupe/ttl_seconds"]}
    result = ReloadResult(
        attempt_id="reload_000000000000000000000001",
        audit_reference="audit_000000000000000000000001",
        outcome=ReloadOutcome.COMMITTED,
        phase=ReloadPhase.COMPLETED,
        disposition=ReloadDisposition.LIVE,
        old_generation=0,
        final_generation=1,
        candidate_reference=candidate.reference,
        candidate_sha256=candidate.candidate_sha256,
        candidate_identity_sha256=candidate.candidate_identity_sha256,
        report_sha256="5" * 64,
        diff_sha256=diff.digest,
        changed_paths=changed_paths,
        message="Committed.",
    )
    warnings = ["warning:" + ("a" * 24)]
    acknowledgment = WarningAcknowledgment(
        actor="operator",
        candidate_sha256=candidate.candidate_sha256,
        candidate_identity_sha256=candidate.candidate_identity_sha256,
        report_sha256="5" * 64,
        active_generation=0,
        warning_identities=warnings,
        acknowledged_at=NOW,
        validator_completed_at=NOW,
        expires_at=NOW + dt.timedelta(seconds=300),
    )
    blockers = ["tts_synthesis"]
    snapshot = SafePointSnapshot(blockers, 0.5)

    candidate_before = candidate.to_dict()
    diff_before = diff.to_dict()
    result_before = result.to_dict()
    acknowledgment_before = acknowledgment.to_dict()
    snapshot_before = snapshot.to_dict()
    digest_before = diff.digest

    source_item["source"] = "mutated.yaml"
    origin_item["path"] = "/mutated"
    environment_item["present"] = True
    old_value["nested"].append("after")
    location["start"]["line"] = 99
    changed_paths["live"].append("/mutated")
    warnings.append("warning:" + ("b" * 24))
    blockers.append("worker_result_promotion")

    assert candidate.to_dict() == candidate_before
    assert diff.to_dict() == diff_before
    assert diff.digest == digest_before
    assert result.to_dict() == result_before
    assert acknowledgment.to_dict() == acknowledgment_before
    assert snapshot.to_dict() == snapshot_before
