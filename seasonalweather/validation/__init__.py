"""Lazy public P1-14 validation API.

The package stays lazy because P1-06/P1-09 Pydantic models import only
``validation.modeling`` and must not create a capability-model import cycle.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AdmissionField": "admission",
    "AdmissionRejection": "admission",
    "CandidateIdentity": "pipeline",
    "canonical_report_sha256": "candidate_identity",
    "CapabilityAnalysis": "capability",
    "CapabilityNeed": "capability",
    "CompatibilityDisposition": "compatibility",
    "CompatibilityIdentity": "compatibility",
    "configured_preflight_probes": "probe_factory",
    "DiagnosticPath": "paths",
    "EnvironmentInputIdentity": "pipeline",
    "FixOperation": "issues",
    "FixSafety": "issues",
    "IntegerRange": "compatibility",
    "VALIDATION_ENVELOPE_SECONDS": "limits",
    "MachineFix": "issues",
    "PathKind": "paths",
    "PolicyDecision": "pipeline",
    "PreflightProbe": "preflight",
    "PreflightResult": "preflight",
    "ProbeExecutor": "preflight",
    "ProbeFailureKind": "preflight",
    "ProbeObservation": "preflight",
    "ProbeRedaction": "preflight",
    "ProbeStatus": "preflight",
    "ReportVerification": "pipeline",
    "StageResult": "pipeline",
    "StageState": "issues",
    "SupportedCompatibility": "compatibility",
    "ValidationContext": "pipeline",
    "ValidationIssue": "issues",
    "ValidationPolicy": "pipeline",
    "ValidationReport": "pipeline",
    "ValidationStage": "issues",
    "ValidatorStamp": "pipeline",
    "admission_error": "admission",
    "analyze_capabilities": "capability",
    "analyze_compatibility": "compatibility",
    "authentication_field": "admission",
    "evaluate_policy": "pipeline",
    "import_feature_field": "admission",
    "job_payload_field": "admission",
    "json_payload_field": "admission",
    "local_path_probe": "preflight",
    "local_executable_probe": "preflight",
    "local_file_separation_probe": "preflight",
    "render_validation_report": "renderer",
    "run_preflight": "preflight",
    "scheduled_insert_field": "admission",
    "segment_field": "admission",
    "tts_field": "admission",
    "upload_field": "admission",
    "validate_compiled": "pipeline",
    "validate_path": "pipeline",
    "validate_wav_upload": "admission",
    "verify_report": "pipeline",
    "verify_report_mapping": "pipeline",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
