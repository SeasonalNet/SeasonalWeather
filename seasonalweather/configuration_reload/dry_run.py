"""Application-owned local dry-run inspection used by the thin CLI adapter."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from pathlib import Path

from seasonalweather.validation import ValidationContext, ValidationPolicy, validate_compiled

from .candidate_store import CandidateStore
from .diff import build_reload_diff
from .models import ReloadDisposition


async def inspect_reload_candidate(active_path: str, candidate_path: str) -> tuple[dict[str, object], int]:
    fixed = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
    with tempfile.TemporaryDirectory(prefix="seasonalweather-config-reload-") as temporary:
        store = CandidateStore(Path(temporary), clock=lambda: fixed)
        active_record, active = store.capture(active_path)
        candidate_record, candidate = store.capture(candidate_path)
        context = ValidationContext(
            active_configuration_generation=0,
            environment_inputs=store.environment_identities(),
            policy=ValidationPolicy(warning_acknowledgment_required=True),
            clock=lambda: fixed,
        )
        report = await validate_compiled(candidate, context=context)
        if not report.decision.valid or candidate.value is None or active.value is None:
            return {
                "outcome": "invalid",
                "valid": False,
                "diagnostic_codes": sorted({issue.code for issue in report.issues}),
            }, 1
        _reference, report_digest = store.store_report(candidate_record, report.to_dict())
        diff = build_reload_diff(
            active,
            candidate,
            active_generation=0,
            active_identity_sha256=active_record.candidate_identity_sha256,
            candidate_identity_sha256=candidate_record.candidate_identity_sha256,
            report_sha256=report_digest,
            active_environment_inputs=active_record.environment_inputs,
            candidate_environment_inputs=candidate_record.environment_inputs,
        )
        warnings = sorted({issue.code for issue in report.issues if issue.severity.value == "warning"})
        outcome, code = _dry_run_outcome(
            effective_change=diff.effective_change,
            disposition=diff.disposition,
            warnings=bool(warnings),
            acknowledgment_required=report.decision.warning_acknowledgment_required,
        )
        return {
            "outcome": outcome,
            "valid": True,
            "disposition": diff.disposition.value,
            "source_only_change": diff.source_only_change,
            "changed_paths": diff.grouped_paths(),
            "warning_codes": warnings,
            "secrets_redacted": all(entry.old != os.environ.get("SEASONAL_API_TOKEN") for entry in diff.entries),
        }, code


def _dry_run_outcome(
    *,
    effective_change: bool,
    disposition: ReloadDisposition,
    warnings: bool,
    acknowledgment_required: bool,
) -> tuple[str, int]:
    if not effective_change:
        return "no_op", 0
    if disposition is ReloadDisposition.RESTART_REQUIRED:
        return "restart_required", 3
    if warnings and acknowledgment_required:
        return "acknowledgment_required", 4
    return "dry_run", 0
