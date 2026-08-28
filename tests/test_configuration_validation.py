from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from collections import Counter
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from seasonalweather.api.models import CreateTextInsertRequest
from seasonalweather.auth.service import AuthenticationError, normalize_scopes
from seasonalweather.configuration import SourceDocument, compile_path, compile_source
from seasonalweather.configuration.schema import SUPPORTED_CONFIG_SCHEMAS
from seasonalweather.configuration.semantic_rules import job_repository_identity_errors
from seasonalweather.configuration.source import CompilerLimits
from seasonalweather.diagnostics.bindings import code_for_rule
from seasonalweather.diagnostics.models import DiagnosticSeverity
from seasonalweather.jobs.policies import JobType
from seasonalweather.jobs.registry import policy_for
from seasonalweather.validation import (
    VALIDATION_ENVELOPE_SECONDS,
    CandidateIdentity,
    CompatibilityDisposition,
    CompatibilityIdentity,
    EnvironmentInputIdentity,
    IntegerRange,
    PreflightProbe,
    ProbeObservation,
    ProbeRedaction,
    ProbeStatus,
    ValidationContext,
    ValidationPolicy,
    ValidationStage,
    ValidatorStamp,
    admission_error,
    analyze_compatibility,
    authentication_field,
    canonical_report_sha256,
    import_feature_field,
    job_payload_field,
    json_payload_field,
    scheduled_insert_field,
    segment_field,
    tts_field,
    upload_field,
    validate_compiled,
    validate_wav_upload,
)
from seasonalweather.validation.admission import AdmissionField
from seasonalweather.validation import (
    verify_report as _verify_report,
)
from seasonalweather.validation import (
    verify_report_mapping as _verify_report_mapping,
)
from seasonalweather.validation.candidate_identity import complete_candidate_sha256
from seasonalweather.validation.paths import DiagnosticPath, PathKind
from seasonalweather.validation.pipeline import (
    ReportVerification,
    VerificationFailure,
    _compatibility_issues,
    default_supported_compatibility,
)
from seasonalweather.validation.preflight import LocalPathSpecification

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/config.yaml"
NOW = dt.datetime(2026, 7, 29, 16, tzinfo=dt.UTC)


def verify_report(report, **kwargs):
    kwargs.setdefault(
        "expected_candidate_identity_sha256",
        report.candidate.identity_sha256,
    )
    kwargs.setdefault("expected_report_sha256", canonical_report_sha256(report.to_dict()))
    return _verify_report(report, **kwargs)


def verify_report_mapping(payload, **kwargs):
    candidate = payload.get("candidate") if isinstance(payload, dict) else None
    identity = candidate.get("identity_sha256") if isinstance(candidate, dict) else "0" * 64
    kwargs.setdefault("expected_candidate_identity_sha256", identity)
    kwargs.setdefault("expected_report_sha256", canonical_report_sha256(payload))
    return _verify_report_mapping(payload, **kwargs)


class _ValidationProbeExecutor:
    def __init__(self, observation: ProbeObservation) -> None:
        self.observation = observation

    async def observe(self, probe, monotonic):
        del probe, monotonic
        return self.observation, None


def _test_probe(
    identifier: str,
    status: ProbeStatus,
    *,
    required: bool,
    fallback_available: bool = False,
    retryable: bool = False,
) -> tuple[PreflightProbe, _ValidationProbeExecutor]:
    probe = PreflightProbe(
        identifier=identifier,
        owner="test",
        timeout_seconds=0.1,
        required=required,
        fallback_available=fallback_available,
        redaction=ProbeRedaction.IDENTIFIER_ONLY,
        specification=LocalPathSpecification("/unused-test-fixture", directory=False),
    )
    return probe, _ValidationProbeExecutor(ProbeObservation(status, "test observation", retryable=retryable))


def _compiled(text: str | None = None):
    data = (text if text is not None else EXAMPLE.read_text(encoding="utf-8")).encode()
    return compile_source(SourceDocument.from_bytes(data, source_id="candidate.yaml"), environ={})


def _report(text: str | None = None, **context):
    return asyncio.run(
        validate_compiled(
            _compiled(text),
            context=ValidationContext(clock=lambda: NOW, **context),
        )
    )


def test_parse_and_schema_failures_skip_later_stages_without_relabeling() -> None:
    parse = _report("station: [\n")
    schema = _report(EXAMPLE.read_text(encoding="utf-8").replace('  name: "SeasonalWeather"\n', "", 1))

    assert parse.stages[0].issues[0].phase is ValidationStage.PARSE
    assert [stage.state.value for stage in parse.stages] == ["completed"] + ["skipped"] * 6
    assert schema.stages[1].issues[0].phase is ValidationStage.SCHEMA
    assert [stage.state.value for stage in schema.stages[:2]] == ["completed", "completed"]
    assert all(stage.state.value == "skipped" for stage in schema.stages[2:])


@pytest.mark.parametrize(
    ("needle", "replacement", "rule"),
    [
        ('backend: "local"', 'backend: "not-a-backend"', "semantic.invariant"),
        ("fallback_backend: null", 'fallback_backend: "not-a-backend"', "semantic.invariant"),
        ("fallback_backend: null", 'fallback_backend: "espeak-ng"', "semantic.invariant"),
        ("fallback_backend: null", 'fallback_backend: "seasonal_ttsd"', "semantic.invariant"),
    ],
)
def test_tts_backend_and_fallback_semantics_are_admitted_before_runtime(
    needle: str, replacement: str, rule: str
) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    text = text.replace(needle, replacement, 1)
    report = _report(text)
    semantic_rules = {
        issue.validator_rule_id
        for stage in report.stages
        if stage.stage is ValidationStage.SEMANTIC
        for issue in stage.issues
    }
    assert rule in semantic_rules


@pytest.mark.parametrize(
    ("provider", "field", "replacement"),
    [
        ("seasonal_ttsd", "base_url", 'base_url: "https://user:secret@tts.example.test"'),
        ("seasonal_ttsd", "base_url", 'base_url: "https://[bad"'),
        ("seasonal_ttsd", "base_url", 'base_url: "https://tts.example.test:abc"'),
        ("seasonal_ttsd", "base_url", 'base_url: "https://tts.example.test:99999"'),
        ("seasonal_ttsd", "base_url", 'base_url: "https://tts.example.test:0"'),
        ("seasonal_ttsd", "client_credential_file", 'client_credential_file: ""'),
        ("seasonal_ttsd", "voice", 'voice: "other-voice"'),
        ("seasonal_ttsd", "profile", 'profile: "other-profile"'),
        ("seasonal_ttsd", "token_ttl_seconds", "token_ttl_seconds: -1"),
        ("seasonal_ttsd", "refresh_margin_seconds", "refresh_margin_seconds: 900"),
        ("seasonal_ttsd", "connect_timeout_seconds", "connect_timeout_seconds: 31.0"),
        ("seasonal_ttsd", "token_timeout_seconds", "token_timeout_seconds: 61.0"),
        ("seasonal_ttsd", "synthesis_timeout_seconds", "synthesis_timeout_seconds: 601.0"),
        ("seasonal_ttsd", "max_input_bytes", "max_input_bytes: 1048577"),
        ("seasonal_ttsd", "max_response_bytes", "max_response_bytes: 134217729"),
        ("seasonal_ttsd", "max_error_bytes", "max_error_bytes: 1048577"),
        ("seasonal_ttsd", "verify_tls", "verify_tls: false"),
        ("openai_compatible", "base_url", 'base_url: "https://api.example.test"'),
        ("openai_compatible", "api_key_file", 'api_key_file: ""'),
        ("openai_compatible", "model", 'model: ""'),
        ("openai_compatible", "voice", 'voice: ""'),
        ("openai_compatible", "response_format", 'response_format: "pcm"'),
        ("openai_compatible", "speed", "speed: 5.0"),
        ("openai_compatible", "connect_timeout_seconds", "connect_timeout_seconds: 31.0"),
        ("openai_compatible", "synthesis_timeout_seconds", "synthesis_timeout_seconds: 601.0"),
        ("openai_compatible", "max_input_bytes", "max_input_bytes: 1048577"),
        ("openai_compatible", "max_response_bytes", "max_response_bytes: 134217729"),
        ("openai_compatible", "max_error_bytes", "max_error_bytes: 1048577"),
        ("openai_compatible", "verify_tls", "verify_tls: false"),
    ],
)
def test_selected_remote_provider_fields_are_source_mapped(provider: str, field: str, replacement: str) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    text = text.replace('  backend: "local"', f'  backend: "{provider}"', 1)
    if provider == "seasonal_ttsd":
        text = text.replace(
            '  seasonal_ttsd:\n    base_url: ""', '  seasonal_ttsd:\n    base_url: "https://tts.example.test"', 1
        )
        text = text.replace(
            '    client_credential_file: ""', '    client_credential_file: "/run/credentials/client"', 1
        )
    else:
        text = text.replace(
            '  openai_compatible:\n    base_url: ""',
            '  openai_compatible:\n    base_url: "https://api.example.test/v1"',
            1,
        )
        text = text.replace('    api_key_file: ""', '    api_key_file: "/run/credentials/api-key"', 1)
        text = text.replace('    model: ""', '    model: "tts-model"', 1)
        text = text.replace('    voice: ""', '    voice: "alloy"', 1)
    value = replacement.removeprefix(f"{field}: ")
    block_end = "  openai_compatible:" if provider == "seasonal_ttsd" else "  volume:"
    pattern = rf"(?ms)(  {provider}:.*?)(?=\n{block_end})"
    block = re.search(pattern, text)
    assert block is not None
    updated = re.sub(rf"(?m)^    {field}: .*?$", f"    {field}: {value}", block.group(1), count=1)
    text = text[: block.start(1)] + updated + text[block.end(1) :]
    report = _report(text)
    assert not report.decision.valid
    assert any(
        issue.path is not None and issue.path.to_pointer() == f"/tts/{provider}/{field}"
        for issue in report.issues
        if issue.phase is ValidationStage.SEMANTIC
    )


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("seasonal_ttsd", "https://tts.example.test"),
        ("seasonal_ttsd", "https://tts.example.test:8443"),
        ("openai_compatible", "https://api.example.test/v1"),
        ("openai_compatible", "https://api.example.test:9443/v1"),
    ],
)
def test_selected_remote_valid_https_origins_are_accepted(provider: str, base_url: str) -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace('  backend: "local"', f'  backend: "{provider}"', 1)
    if provider == "seasonal_ttsd":
        text = text.replace('  seasonal_ttsd:\n    base_url: ""', f'  seasonal_ttsd:\n    base_url: "{base_url}"', 1)
        text = text.replace(
            '    client_credential_file: ""', '    client_credential_file: "/run/credentials/client"', 1
        )
    else:
        text = text.replace(
            '  openai_compatible:\n    base_url: ""', f'  openai_compatible:\n    base_url: "{base_url}"', 1
        )
        text = text.replace('    api_key_file: ""', '    api_key_file: "/run/credentials/api-key"', 1)
        text = text.replace('    model: ""', '    model: "tts-model"', 1)
        text = text.replace('    voice: ""', '    voice: "alloy"', 1)
    report = _report(text)
    assert report.decision.valid


@pytest.mark.parametrize(
    ("needle", "replacement", "pointer"),
    (
        (
            "  text_overrides: []",
            '  text_overrides:\n    - match: "(a+)+$"\n      replace: "x"\n      regex: true',
            "/tts/text_overrides/0/match",
        ),
        (
            "      alias_overrides: []",
            '      alias_overrides:\n        - match: "a*?"\n          alias: "unsafe"\n          regex: true',
            "/tts/local/voicetext_paul/alias_overrides/0/match",
        ),
    ),
)
def test_tts_configured_regexes_fail_during_semantic_validation_with_source_paths(
    needle: str, replacement: str, pointer: str
) -> None:
    report = _report(EXAMPLE.read_text(encoding="utf-8").replace(needle, replacement, 1))
    issues = [
        issue
        for stage in report.stages
        if stage.stage is ValidationStage.SEMANTIC
        for issue in stage.issues
        if issue.path is not None
    ]
    assert any(issue.path.to_pointer() == pointer for issue in issues)


def test_legacy_voicetext_configuration_location_is_validated() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "  volume: 1.0",
        '  voicetext_paul:\n    alias_overrides:\n      - match: "a{1,256}a+"\n        alias: "unsafe"\n        regex: true\n  volume: 1.0',
        1,
    )
    report = _report(text)
    assert any(
        issue.path is not None and issue.path.to_pointer() == "/tts/voicetext_paul/alias_overrides/0/match"
        for stage in report.stages
        if stage.stage is ValidationStage.SEMANTIC
        for issue in stage.issues
    )


def test_ignore_case_regex_overlap_is_rejected_during_semantic_configuration_validation() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "  text_overrides: []",
        '  text_overrides:\n    - match: "a{1,256}A{1,256}b"\n      replace: "x"\n      regex: true\n      ignore_case: true',
        1,
    )
    report = _report(text)
    assert any(
        issue.path is not None
        and issue.path.to_pointer() == "/tts/text_overrides/0/match"
        and stage.stage is ValidationStage.SEMANTIC
        for stage in report.stages
        for issue in stage.issues
    )


def test_replacement_backreference_is_rejected_during_semantic_validation() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "  text_overrides: []",
        '  text_overrides:\n    - match: "NWS"\n      replace: "\\\\1"',
        1,
    )
    report = _report(text)
    assert any(
        issue.path is not None
        and issue.path.to_pointer() == "/tts/text_overrides/0/replace"
        and stage.stage is ValidationStage.SEMANTIC
        for stage in report.stages
        for issue in stage.issues
    )


def test_preflight_readiness_requires_a_completed_evaluation_in_typed_and_external_policy() -> None:
    semantic_text = EXAMPLE.read_text(encoding="utf-8").replace("  total_seconds: 30.0", "  total_seconds: 4.0", 1)
    incompatible = CompatibilityIdentity(
        software_version="0.17.0",
        build_identity=None,
        validation_protocol_version=1,
        config_schema_version=1,
        swwp_protocol_version=2,
        job_payload_schema_versions=(1,),
        job_result_schema_versions=(1,),
        diagnostic_schema_version=1,
        diagnostic_catalog_version=1,
        capability_manifest_version=1,
        report_schema_version=1,
    )
    healthy_probe, healthy_executor = _test_probe("healthy", ProbeStatus.AVAILABLE, required=True)
    blocking_probe, blocking_executor = _test_probe(
        "blocking",
        ProbeStatus.UNAVAILABLE,
        required=True,
        retryable=True,
    )
    cases = (
        ("preflight not requested", _report(), False),
        ("parse failure", _report("station: [\n"), False),
        (
            "schema failure",
            _report(EXAMPLE.read_text(encoding="utf-8").replace('  name: "SeasonalWeather"\n', "", 1)),
            False,
        ),
        ("semantic failure", _report(semantic_text, preflight_enabled=True), False),
        ("compatibility failure", _report(compatibility_identity=incompatible, preflight_enabled=True), False),
        (
            "completed healthy preflight",
            _report(
                preflight_enabled=True,
                preflight_probes=(healthy_probe,),
                preflight_executor=healthy_executor,
            ),
            True,
        ),
        (
            "completed blocking preflight",
            _report(
                preflight_enabled=True,
                preflight_probes=(blocking_probe,),
                preflight_executor=blocking_executor,
            ),
            False,
        ),
    )

    for name, report, expected_ready in cases:
        assert report.decision.preflight_ready is expected_ready, name
        assert report.decision.acceptable_for_reload_decision is (report.decision.valid and expected_ready), name
        verification = verify_report(
            report,
            expected_candidate_sha256=report.candidate.sha256,
        )
        assert verification.accepted, name


@pytest.mark.parametrize(
    ("old", "new", "pointer"),
    [
        ("      minimum_ttl_seconds: 60", "      minimum_ttl_seconds: 1000", "/api/auth/exchange/minimum_ttl_seconds"),
        (
            "  enabled: true\n  required: true",
            "  enabled: false\n  required: true",
            "/jobs/required",
        ),
        (
            "\n  assignment_ack_seconds: 10\n",
            "\n  assignment_ack_seconds: 60\n",
            "/jobs/assignment_ack_seconds",
        ),
        ("  total_seconds: 30.0", "  total_seconds: 4.0", "/lifecycle/total_seconds"),
    ],
)
def test_cross_field_semantic_errors_are_source_addressed(
    old: str,
    new: str,
    pointer: str,
) -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace(old, new, 1)
    report = _report(text)
    issue = next(item for item in report.issues if item.phase is ValidationStage.SEMANTIC)

    assert issue.code == "SWCFG2002"
    assert issue.path is not None and issue.path.to_pointer() == pointer
    assert issue.primary is not None
    assert issue.related
    assert report.decision.valid is False


def test_default_candidate_satisfies_every_semantic_invariant() -> None:
    report = _report()

    assert not [item for item in report.issues if item.phase is ValidationStage.SEMANTIC]


def test_repository_path_equivalence_is_lexical_and_cwd_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = job_repository_identity_errors(
        enabled=True,
        required=False,
        path="state/../database.sqlite3",
        operational_database_path="database.sqlite3",
    )
    monkeypatch.chdir(tmp_path)
    second = job_repository_identity_errors(
        enabled=True,
        required=False,
        path="state/../database.sqlite3",
        operational_database_path="database.sqlite3",
    )

    assert first == second == ("jobs.path must be separate from database.path",)


def test_lifecycle_fix_is_deterministic_fenced_and_never_applied() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace("  total_seconds: 30.0", "  total_seconds: 4.0", 1)
    compiled = _compiled(text)
    before = compiled.source.text if compiled.source else ""
    first = asyncio.run(validate_compiled(compiled, context=ValidationContext(clock=lambda: NOW)))
    second = asyncio.run(validate_compiled(compiled, context=ValidationContext(clock=lambda: NOW)))
    issue = next(item for item in first.issues if item.fixes)
    fix = issue.fixes[0]

    assert fix.to_dict() == next(item for item in second.issues if item.fixes).fixes[0].to_dict()
    assert fix.expected_old_value == 4.0
    assert compiled.source is not None
    assert fix.expected_source_sha256 == compiled.source.digest
    assert fix.replacement == 10.0
    assert compiled.source is not None and compiled.source.text == before


def test_auth_conflicts_use_file_and_environment_origins_without_secret_fixes() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "  allow_remote: false",
        '  subject: "legacy"\n  allow_remote: false',
        1,
    )
    file_report = _report(text)
    compiled = compile_source(
        SourceDocument.from_bytes(EXAMPLE.read_bytes(), source_id="candidate.yaml"),
        environ={
            "SEASONAL_API_TOKEN": "SENTINEL-PRIMARY",
            "SEASONAL_API_TOKENS_JSON": "SENTINEL-SECONDARY",
        },
    )
    environment_report = asyncio.run(validate_compiled(compiled, context=ValidationContext(clock=lambda: NOW)))
    environment_issue = next(
        item
        for item in environment_report.issues
        if item.path is not None and item.path.to_pointer() == "/secrets/api_token"
    )

    assert any(item.related for item in file_report.issues if item.phase is ValidationStage.SEMANTIC)
    assert environment_issue.redacted
    assert not environment_issue.fixes
    assert "SENTINEL" not in environment_report.to_json()
    assert not environment_report.candidate.reproducible


def test_default_and_generated_origins_do_not_fabricate_semantic_spans() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "lifecycle:\n"
        "  total_seconds: 30.0\n"
        "  active_request_seconds: 10.0\n"
        "  publication_seconds: 8.0\n"
        "  source_stop_seconds: 8.0\n"
        "  tts_stop_seconds: 8.0\n"
        "  task_cancel_seconds: 5.0\n"
        "  resource_close_seconds: 5.0\n"
        "  optional_tasks:\n"
        "    # never = leave degraded, restart = bounded recovery, always = retry\n"
        "    # after each cooldown. Drain/cancel always suppresses restart.\n"
        '    policy: "restart"\n'
        "    stable_after_seconds: 60.0\n"
        "    restart_initial_delay_seconds: 1.0\n"
        "    restart_max_delay_seconds: 30.0\n"
        "    thrash_window_seconds: 300.0\n"
        "    thrash_limit: 3\n"
        "    cooldown_seconds: 300.0\n",
        "",
        1,
    )
    report = _report(text)

    assert report.decision.valid
    assert not any(item.path and item.path.to_pointer().startswith("/lifecycle") for item in report.issues)


def test_deprecation_and_suggestion_are_separate_nonblocking_phases() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace('  backend: "local"', '  backend: "espeak_ng"', 1)
    report = _report(text)

    deprecation = next(item for item in report.issues if item.phase is ValidationStage.DEPRECATION)
    suggestion = next(item for item in report.issues if item.severity is DiagnosticSeverity.SUGGESTION)
    assert deprecation.fixes[0].operation.value == "remove"
    assert deprecation.fixes[0].expected_source_sha256 == report.candidate.source_manifest[0].sha256
    assert suggestion.phase is ValidationStage.ADVISORY
    assert suggestion.fixes[0].replacement == "espeak-ng"
    assert report.decision.valid


def test_compatibility_failure_is_not_a_preflight_outage() -> None:
    current = CompatibilityIdentity(
        software_version="0.17.0",
        build_identity=None,
        validation_protocol_version=1,
        config_schema_version=1,
        swwp_protocol_version=2,
        job_payload_schema_versions=(1,),
        job_result_schema_versions=(1,),
        diagnostic_schema_version=1,
        diagnostic_catalog_version=1,
        capability_manifest_version=1,
        report_schema_version=1,
    )
    report = _report(compatibility_identity=current, preflight_enabled=True)

    assert any(item.phase is ValidationStage.COMPATIBILITY for item in report.issues)
    preflight = report.stages[-1]
    assert preflight.state.value == "skipped"
    assert "compatibility" in (preflight.skipped_reason or "").lower()


def test_candidate_hash_uses_exact_bytes_and_deterministic_bundle_order() -> None:
    first = CandidateIdentity.from_source_bundle((("candidate.yaml", b"a: 1\n"),), config_schema_version=1)
    comment = CandidateIdentity.from_source_bundle((("candidate.yaml", b"a: 1\n# x\n"),), config_schema_version=1)
    ordered = CandidateIdentity.from_source_bundle(
        (("b.yaml", b"b"), ("a.yaml", b"a")),
        config_schema_version=1,
    )
    reversed_bundle = CandidateIdentity.from_source_bundle(
        (("a.yaml", b"a"), ("b.yaml", b"b")),
        config_schema_version=1,
    )
    unavailable = CandidateIdentity.from_source_bundle((("candidate.yaml", None),), config_schema_version=1)
    compiled = CandidateIdentity.from_compiled(_compiled())
    bundled_example = CandidateIdentity.from_source_bundle(
        (("candidate.yaml", EXAMPLE.read_bytes()),),
        config_schema_version=1,
    )

    assert first.sha256 != comment.sha256
    assert first.source_manifest[0].sha256 == __import__("hashlib").sha256(b"a: 1\n").hexdigest()
    assert first.source_manifest[0].byte_length == 5
    assert ordered == reversed_bundle
    assert compiled.sha256 == bundled_example.sha256
    assert compiled.source_manifest == bundled_example.source_manifest
    assert tuple(item.source for item in ordered.source_manifest) == ("a.yaml", "b.yaml")
    assert tuple(item.byte_length for item in ordered.source_manifest) == (1, 1)
    assert unavailable.sha256 is None
    assert unavailable.source_manifest[0].source == "candidate.yaml"
    assert unavailable.source_manifest[0].byte_length is None
    assert not unavailable.reproducible


def test_compatibility_ranges_sets_and_semver_are_explicit() -> None:
    supported = default_supported_compatibility()
    malformed = CompatibilityIdentity(
        software_version="not-semver",
        build_identity=None,
        validation_protocol_version=1,
        config_schema_version=1,
        swwp_protocol_version=1,
        job_payload_schema_versions=(1, 1),
        job_result_schema_versions=(1,),
        diagnostic_schema_version=1,
        diagnostic_catalog_version=1,
        capability_manifest_version=1,
        report_schema_version=1,
    )
    findings = {item.field: item for item in analyze_compatibility(malformed, supported)}

    assert IntegerRange(2, 3).classify(1) is CompatibilityDisposition.UNSUPPORTED_OLDER
    assert IntegerRange(2, 3).classify(4) is CompatibilityDisposition.UNSUPPORTED_NEWER
    assert findings["software_version"].disposition is CompatibilityDisposition.MALFORMED
    assert findings["job_payload_schema_versions"].disposition is CompatibilityDisposition.CONTRADICTORY


@pytest.mark.parametrize(
    ("field", "changes"),
    [
        ("validation_protocol_version", {"validation_protocol_version": 2}),
        ("config_schema_version", {"config_schema_version": 2}),
        ("swwp_protocol_version", {"swwp_protocol_version": 2}),
        ("job_payload_schema_versions", {"job_payload_schema_versions": (2,)}),
        ("job_result_schema_versions", {"job_result_schema_versions": (2,)}),
        ("diagnostic_schema_version", {"diagnostic_schema_version": 2}),
        ("diagnostic_catalog_version", {"diagnostic_catalog_version": 2}),
        ("capability_manifest_version", {"capability_manifest_version": 2}),
        ("report_schema_version", {"report_schema_version": 2}),
    ],
)
def test_each_version_identity_reports_unsupported_newer(field: str, changes: dict[str, object]) -> None:
    current = CompatibilityIdentity(
        software_version="0.17.0",
        build_identity=None,
        validation_protocol_version=1,
        config_schema_version=1,
        swwp_protocol_version=1,
        job_payload_schema_versions=(1,),
        job_result_schema_versions=(1,),
        diagnostic_schema_version=1,
        diagnostic_catalog_version=1,
        capability_manifest_version=1,
        report_schema_version=1,
    )
    finding = next(
        item
        for item in analyze_compatibility(replace(current, **changes), default_supported_compatibility())
        if item.field == field
    )

    assert finding.disposition is CompatibilityDisposition.UNSUPPORTED_NEWER


def test_software_compatibility_distinguishes_advisory_missing_and_older() -> None:
    current = CompatibilityIdentity(
        software_version="0.18.0",
        build_identity=None,
        validation_protocol_version=1,
        config_schema_version=1,
        swwp_protocol_version=1,
        job_payload_schema_versions=(1,),
        job_result_schema_versions=(1,),
        diagnostic_schema_version=1,
        diagnostic_catalog_version=1,
        capability_manifest_version=1,
        report_schema_version=1,
    )
    supported = replace(
        default_supported_compatibility(),
        software_minimum="0.17.0",
        software_maximum_exclusive="1.0.0",
        swwp_protocol=IntegerRange(2, 3),
    )
    findings = {item.field: item for item in analyze_compatibility(current, supported)}
    missing = {item.field: item for item in analyze_compatibility(replace(current, software_version=None), supported)}

    assert findings["software_version"].disposition is CompatibilityDisposition.ADVISORY
    assert findings["swwp_protocol_version"].disposition is CompatibilityDisposition.UNSUPPORTED_OLDER
    assert missing["software_version"].disposition is CompatibilityDisposition.MISSING
    software_issue = next(
        issue
        for issue in _compatibility_issues(current, supported, ())
        if issue.path is not None and issue.path.to_pointer() == "/validator_stamp/software_version"
    )
    assert software_issue.code == "SWCFG0003"
    assert software_issue.severity is DiagnosticSeverity.SUGGESTION
    assert not software_issue.blocking


def test_software_compatibility_accepts_vcs_development_versions() -> None:
    current = CompatibilityIdentity(
        software_version="0.18.0.dev55+g921616b15.d20260827",
        build_identity=None,
        validation_protocol_version=1,
        config_schema_version=1,
        swwp_protocol_version=1,
        job_payload_schema_versions=(1,),
        job_result_schema_versions=(1,),
        diagnostic_schema_version=1,
        diagnostic_catalog_version=1,
        capability_manifest_version=1,
        report_schema_version=1,
    )
    supported = replace(
        default_supported_compatibility(),
        software_minimum="0.17.0",
        software_maximum_exclusive="1.0.0",
    )

    finding = analyze_compatibility(current, supported)[0]

    assert finding.disposition is CompatibilityDisposition.ADVISORY


def test_software_compatibility_orders_development_builds_before_release() -> None:
    supported = replace(
        default_supported_compatibility(),
        software_minimum="0.18.0.dev3",
        software_maximum_exclusive="0.18.0",
    )
    base = CompatibilityIdentity(
        software_version="0.18.0.dev4+gabc",
        build_identity=None,
        validation_protocol_version=1,
        config_schema_version=1,
        swwp_protocol_version=1,
        job_payload_schema_versions=(1,),
        job_result_schema_versions=(1,),
        diagnostic_schema_version=1,
        diagnostic_catalog_version=1,
        capability_manifest_version=1,
        report_schema_version=1,
    )

    assert analyze_compatibility(base, supported)[0].disposition is CompatibilityDisposition.COMPATIBLE
    assert (
        analyze_compatibility(replace(base, software_version="0.18.0"), supported)[0].disposition
        is CompatibilityDisposition.UNSUPPORTED_NEWER
    )


@pytest.mark.parametrize(
    "version",
    (
        "1.0.0-01",
        "1.0.0-alpha..1",
        "1.0.0-alpha_1",
        "1.0",
        "01.0.0",
        "1.0.0+build..1",
    ),
)
def test_semver_rejects_malformed_identifiers(version: str) -> None:
    identity = replace(
        current := CompatibilityIdentity(
            software_version=version,
            build_identity=None,
            validation_protocol_version=1,
            config_schema_version=1,
            swwp_protocol_version=1,
            job_payload_schema_versions=(1,),
            job_result_schema_versions=(1,),
            diagnostic_schema_version=1,
            diagnostic_catalog_version=1,
            capability_manifest_version=1,
            report_schema_version=1,
        ),
        software_version=version,
    )
    finding = analyze_compatibility(identity, default_supported_compatibility())[0]

    assert current.software_version == version
    assert finding.disposition is CompatibilityDisposition.MALFORMED


def test_semver_prerelease_precedence_is_spec_ordered() -> None:
    supported = replace(
        default_supported_compatibility(),
        software_minimum="1.0.0-alpha.1",
        software_maximum_exclusive="1.0.0",
    )
    base = CompatibilityIdentity(
        software_version="1.0.0-alpha.beta",
        build_identity=None,
        validation_protocol_version=1,
        config_schema_version=1,
        swwp_protocol_version=1,
        job_payload_schema_versions=(1,),
        job_result_schema_versions=(1,),
        diagnostic_schema_version=1,
        diagnostic_catalog_version=1,
        capability_manifest_version=1,
        report_schema_version=1,
    )
    inside = analyze_compatibility(base, supported)[0]
    older = analyze_compatibility(replace(base, software_version="1.0.0-alpha"), supported)[0]
    release = analyze_compatibility(replace(base, software_version="1.0.0"), supported)[0]

    assert inside.disposition.compatible
    assert older.disposition is CompatibilityDisposition.UNSUPPORTED_OLDER
    assert release.disposition is CompatibilityDisposition.UNSUPPORTED_NEWER


def test_stamp_report_json_and_defensive_immutability_are_deterministic() -> None:
    context = ValidationContext(
        clock=lambda: NOW,
        environment_inputs=(EnvironmentInputIdentity("SEASONAL_API_TOKEN", False),),
    )
    first = asyncio.run(validate_compiled(_compiled(), context=context))
    second = asyncio.run(validate_compiled(_compiled(), context=context))

    assert first.to_json() == second.to_json()
    assert json.loads(first.to_json())["validator_stamp"]["candidate_sha256"] == first.candidate.sha256
    assert first.candidate.reproducible
    assert verify_report_mapping(
        json.loads(first.to_json()),
        expected_candidate_sha256=first.candidate.sha256,
    ).accepted
    with pytest.raises(FrozenInstanceError):
        setattr(first.candidate, "sha256", "0" * 64)


def test_report_verification_rejects_hash_staleness_and_contradiction() -> None:
    report = _report(active_configuration_generation=7)
    mismatched = verify_report(report, expected_candidate_sha256="0" * 64)
    stale = verify_report(
        report,
        expected_candidate_sha256=report.candidate.sha256,
        current_active_generation=8,
        require_fresh_generation=True,
    )
    contradictory = replace(report, decision=replace(report.decision, valid=not report.decision.valid))
    contradiction = verify_report(
        contradictory,
        expected_candidate_sha256=report.candidate.sha256,
    )
    incompatible_stamp = verify_report(
        replace(report, stamp=replace(report.stamp, validation_protocol_version=2)),
        expected_candidate_sha256=report.candidate.sha256,
    )
    contradictory_range = verify_report(
        replace(report, stamp=replace(report.stamp, supported_config_schema=IntegerRange(1, 2))),
        expected_candidate_sha256=report.candidate.sha256,
    )

    assert mismatched.diagnostic_code == "SWCFG2004"
    assert VerificationFailure.CANDIDATE_MISMATCH in mismatched.failures
    assert VerificationFailure.STALE_GENERATION in stale.failures
    assert VerificationFailure.CONTRADICTORY_REPORT in contradiction.failures
    assert VerificationFailure.INCOMPATIBLE_STAMP in incompatible_stamp.failures
    assert VerificationFailure.INCOMPATIBLE_STAMP in contradictory_range.failures


def test_report_mapping_fails_closed_for_future_phase_severity_and_fix() -> None:
    payload = {
        "validation_report_version": 1,
        "stages": [{"stage": stage.value, "state": "completed", "issues": []} for stage in ValidationStage],
    }
    payload["stages"][2]["issues"] = [
        {
            "phase": "future",
            "severity": "future",
            "fixes": [{"operation": "execute"}],
        }
    ]

    verification = verify_report_mapping(payload, expected_candidate_sha256="0" * 64)
    assert not verification.accepted
    assert verification.diagnostic_code == "SWCFG2004"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stamp_version", 2),
        ("software_version", "1.0.0"),
        ("selected_config_schema", 2),
        ("swwp_protocol_version", 2),
        ("job_payload_schema_versions", [2]),
        ("job_result_schema_versions", [2]),
        ("diagnostic_schema_version", 2),
        ("diagnostic_catalog_version", 2),
        ("capability_manifest_version", 2),
    ],
)
def test_report_mapping_rejects_future_or_incompatible_stamp_fields(field: str, value: object) -> None:
    report = _report()
    payload = json.loads(report.to_json())
    payload["validator_stamp"][field] = value

    assert not verify_report_mapping(
        payload,
        expected_candidate_sha256=report.candidate.sha256,
    ).accepted


def test_report_mapping_rejects_missing_stamp_field_and_impossible_stage_order() -> None:
    report = _report()
    missing = json.loads(report.to_json())
    missing["validator_stamp"].pop("candidate_sha256")
    reordered = json.loads(_report().to_json())
    reordered["stages"][0], reordered["stages"][1] = reordered["stages"][1], reordered["stages"][0]

    assert (
        VerificationFailure.MALFORMED_REPORT
        in verify_report_mapping(
            missing,
            expected_candidate_sha256=report.candidate.sha256,
        ).failures
    )
    assert (
        VerificationFailure.CONTRADICTORY_REPORT
        in verify_report_mapping(
            reordered,
            expected_candidate_sha256=report.candidate.sha256,
        ).failures
    )


def test_rejected_source_bytes_keep_distinct_exact_or_explicitly_missing_identity(
    tmp_path: Path,
) -> None:
    invalid_utf8 = tmp_path / "invalid.yaml"
    invalid_utf8.write_bytes(b"\xff\xfe")
    oversized = tmp_path / "oversized.yaml"
    oversized.write_bytes(b"12345")

    invalid_compiled = compile_path(invalid_utf8)
    oversized_compiled = compile_path(
        oversized,
        limits=CompilerLimits(max_source_bytes=4),
    )
    missing_compiled = compile_path(tmp_path / "missing.yaml")
    invalid_report = asyncio.run(validate_compiled(invalid_compiled))
    oversized_report = asyncio.run(validate_compiled(oversized_compiled))
    missing_report = asyncio.run(validate_compiled(missing_compiled))

    assert invalid_report.candidate.source_manifest[0].sha256 == __import__("hashlib").sha256(b"\xff\xfe").hexdigest()
    assert oversized_report.candidate.source_manifest[0].sha256 == __import__("hashlib").sha256(b"12345").hexdigest()
    assert invalid_report.candidate.source_manifest[0].byte_length == 2
    assert oversized_report.candidate.source_manifest[0].byte_length == 5
    assert invalid_report.candidate.sha256 != oversized_report.candidate.sha256
    assert missing_report.candidate.sha256 is None
    assert not missing_report.candidate.reproducible
    assert missing_report.candidate.source_manifest[0].bytes_available is False

    for report in (invalid_report, oversized_report, missing_report):
        assert verify_report(
            report,
            expected_candidate_sha256=report.candidate.sha256,
        ).accepted


def test_two_source_candidate_identity_round_trips_in_both_orders_and_external_admission() -> None:
    forward = CandidateIdentity.from_source_bundle(
        (("second.yaml", b"second\xff"), ("first.yaml", b"first\n")),
        config_schema_version=1,
    )
    reverse = CandidateIdentity.from_source_bundle(
        (("first.yaml", b"first\n"), ("second.yaml", b"second\xff")),
        config_schema_version=1,
    )
    assert forward == reverse

    clean_text = "\n".join(
        line for line in EXAMPLE.read_text(encoding="utf-8").splitlines() if "max_chars_heightened" not in line
    )
    clean_text += "\n"
    base = _report(clean_text)
    candidate = CandidateIdentity(
        forward.sha256,
        base.candidate.config_schema_version,
        forward.source_manifest,
        base.candidate.origin_manifest,
        base.candidate.environment_inputs,
        reproducible=True,
    )
    stamp = replace(
        base.stamp,
        candidate_sha256=candidate.sha256,
        candidate_identity_sha256=candidate.identity_sha256,
    )
    trusted = replace(base, candidate=candidate, stamp=stamp)

    assert verify_report(
        trusted,
        expected_candidate_sha256=candidate.sha256,
    ).accepted


def test_external_report_verifier_rejects_coordinated_hash_and_binding_tampering() -> None:
    report = _report()
    expected = report.candidate.sha256
    assert expected is not None

    coordinated_hash = json.loads(report.to_json())
    coordinated_hash["candidate"]["sha256"] = "1" * 64
    coordinated_hash["candidate"]["source_manifest"][0]["sha256"] = "1" * 64
    coordinated_hash["validator_stamp"]["candidate_sha256"] = "1" * 64

    schema = json.loads(report.to_json())
    schema["candidate"]["config_schema_version"] = 2
    schema["validator_stamp"]["selected_config_schema"] = 2

    environment = json.loads(report.to_json())
    environment["candidate"]["environment_inputs"].pop()

    source = json.loads(report.to_json())
    source["candidate"]["source_manifest"][0]["bytes_available"] = False

    for payload in (coordinated_hash, schema, environment, source):
        assert not verify_report_mapping(
            payload,
            expected_candidate_sha256=expected,
        ).accepted


def test_external_report_verifier_recomputes_stages_rules_policy_and_decision() -> None:
    report = _report()
    expected = report.candidate.sha256
    assert expected is not None
    payloads: list[dict[str, object]] = []

    timestamp = json.loads(report.to_json())
    timestamp["validator_stamp"]["completed_at"] = "2026-07-29T15:59:59+00:00"
    payloads.append(timestamp)

    skipped = json.loads(report.to_json())
    skipped["stages"][0]["state"] = "skipped"
    skipped["stages"][0]["skipped_reason"] = "tampered"
    skipped["stages"][0]["issues"] = []
    payloads.append(skipped)

    rules = json.loads(report.to_json())
    rules["validator_stamp"]["rule_identities"].pop()
    payloads.append(rules)

    policy = json.loads(report.to_json())
    policy["policy"]["warning_blocks"] = 1
    payloads.append(policy)

    decision = json.loads(report.to_json())
    decision["summary"]["valid"] = not decision["summary"]["valid"]
    payloads.append(decision)

    top = json.loads(report.to_json())
    top["parse_valid"] = False
    payloads.append(top)

    for payload in payloads:
        verification = verify_report_mapping(
            payload,
            expected_candidate_sha256=expected,
        )
        assert not verification.accepted


def test_validation_envelope_is_shared_by_typed_stamp_and_external_verifier() -> None:
    report = _report()
    expected = report.candidate.sha256
    assert expected is not None
    boundary = report.stamp.started_at + dt.timedelta(seconds=VALIDATION_ENVELOPE_SECONDS)

    boundary_stamp = replace(report.stamp, completed_at=boundary)
    assert isinstance(boundary_stamp, ValidatorStamp)
    boundary_report = replace(report, stamp=boundary_stamp)
    assert verify_report(boundary_report, expected_candidate_sha256=expected).accepted

    with pytest.raises(ValueError, match="validation envelope"):
        replace(
            report.stamp,
            completed_at=boundary + dt.timedelta(microseconds=1),
        )

    boundary_payload = json.loads(report.to_json())
    boundary_payload["validator_stamp"]["completed_at"] = boundary.isoformat()
    assert verify_report_mapping(boundary_payload, expected_candidate_sha256=expected).accepted

    overlong_payload = json.loads(report.to_json())
    overlong_payload["validator_stamp"]["completed_at"] = (boundary + dt.timedelta(microseconds=1)).isoformat()
    verification = verify_report_mapping(overlong_payload, expected_candidate_sha256=expected)
    assert VerificationFailure.CONTRADICTORY_REPORT in verification.failures


def test_validation_envelope_covers_final_clock_and_report_emission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_now = 0.0
    clock_calls = 0

    def monotonic() -> float:
        return monotonic_now

    def clock() -> dt.datetime:
        nonlocal clock_calls, monotonic_now
        clock_calls += 1
        if clock_calls == 2:
            monotonic_now = VALIDATION_ENVELOPE_SECONDS + 0.001
        return NOW

    import seasonalweather.validation.pipeline as pipeline

    monkeypatch.setattr(pipeline.time, "monotonic", monotonic)
    with pytest.raises(TimeoutError, match="execution envelope"):
        asyncio.run(validate_compiled(_compiled(), context=ValidationContext(clock=clock)))

    assert clock_calls == 2


def _resummarize_report_mapping(payload: dict[str, Any]) -> None:
    issues = [issue for stage in payload["stages"] for issue in stage["issues"]]
    payload["issues"] = issues
    validation_phases = {"parse", "schema", "semantic", "compatibility"}
    warning = any(issue["severity"] == "warning" for issue in issues)
    valid = not any(issue["blocking"] and issue["phase"] in validation_phases for issue in issues)
    valid = valid and not (payload["policy"]["warning_blocks"] and warning)
    preflight = payload["stages"][-1]
    ready = (
        preflight["state"] == "completed"
        and not any(issue["blocking"] and issue["phase"] == "preflight" for issue in issues)
        and not any(probe["blocking"] for probe in preflight.get("probe_results", []))
    )
    acknowledgment = warning and payload["policy"]["warning_acknowledgment_required"]
    severity = Counter(issue["severity"] for issue in issues)
    blocking = Counter("blocking" if issue["blocking"] else "nonblocking" for issue in issues)
    severity_order = {"info": 0, "suggestion": 1, "deprecation": 2, "warning": 3, "error": 4}
    highest = max((issue["severity"] for issue in issues), key=severity_order.__getitem__, default=None)
    payload["valid"] = valid
    payload["preflight_ready"] = ready
    payload["parse_valid"] = not any(issue["blocking"] for issue in payload["stages"][0]["issues"])
    payload["schema_valid"] = payload["stages"][1]["state"] == "completed" and not any(
        issue["blocking"] for issue in payload["stages"][1]["issues"]
    )
    payload["summary"] = {
        "valid": valid,
        "preflight_ready": ready,
        "warning_acknowledgment_required": acknowledgment,
        "acceptable_for_reload_decision": valid and ready and not acknowledgment,
        "highest_severity": highest,
        "severity_counts": dict(sorted(severity.items())),
        "blocking_counts": dict(sorted(blocking.items())),
        "skipped_stages": [
            {"stage": stage["stage"], "reason": stage["skipped_reason"]}
            for stage in payload["stages"]
            if stage["state"] == "skipped"
        ],
    }


def test_independent_complete_report_binding_rejects_coordinated_semantic_tampering() -> None:
    semantic = _report(EXAMPLE.read_text(encoding="utf-8").replace("  total_seconds: 30.0", "  total_seconds: 4.0", 1))
    removals = json.loads(semantic.to_json())
    additions = json.loads(semantic.to_json())
    removals["stages"][2]["issues"] = []
    additions["stages"][2]["issues"].append(dict(additions["stages"][2]["issues"][0]))

    for payload in (removals, additions):
        _resummarize_report_mapping(payload)
        assert verify_report_mapping(
            payload,
            expected_candidate_sha256=semantic.candidate.sha256,
        ).accepted
        verification = _verify_report_mapping(
            payload,
            expected_candidate_sha256=semantic.candidate.sha256,
            expected_candidate_identity_sha256=semantic.candidate.identity_sha256,
            expected_report_sha256=canonical_report_sha256(semantic.to_dict()),
        )
        assert VerificationFailure.REPORT_MISMATCH in verification.failures


def test_independent_complete_report_binding_rejects_coordinated_advisory_and_issue_tampering() -> None:
    report = _report(EXAMPLE.read_text(encoding="utf-8").replace('  backend: "local"', '  backend: "espeak_ng"', 1))
    payloads: list[dict[str, Any]] = []

    remove_deprecation = json.loads(report.to_json())
    remove_deprecation["stages"][4]["issues"].pop()
    payloads.append(remove_deprecation)

    add_deprecation = json.loads(report.to_json())
    add_deprecation["stages"][4]["issues"].append(dict(add_deprecation["stages"][4]["issues"][-1]))
    payloads.append(add_deprecation)

    remove_advisory = json.loads(report.to_json())
    remove_advisory["stages"][5]["issues"] = []
    payloads.append(remove_advisory)

    add_advisory = json.loads(report.to_json())
    add_advisory["stages"][5]["issues"].append(dict(add_advisory["stages"][5]["issues"][0]))
    payloads.append(add_advisory)

    issue_text = json.loads(report.to_json())
    issue = issue_text["stages"][4]["issues"][0]
    issue["message"] = "Coordinated alternative issue message."
    issue["notes"] = ["Coordinated alternative note."]
    issue["help"] = "Coordinated alternative help."
    issue["retryable"] = True
    issue["fixes"][0].pop("location", None)
    payloads.append(issue_text)

    expected_report = canonical_report_sha256(report.to_dict())
    for payload in payloads:
        _resummarize_report_mapping(payload)
        assert verify_report_mapping(payload, expected_candidate_sha256=report.candidate.sha256).accepted
        verification = _verify_report_mapping(
            payload,
            expected_candidate_sha256=report.candidate.sha256,
            expected_candidate_identity_sha256=report.candidate.identity_sha256,
            expected_report_sha256=expected_report,
        )
        assert VerificationFailure.REPORT_MISMATCH in verification.failures


def test_independent_complete_report_binding_rejects_coordinated_policy_and_probe_tampering() -> None:
    probe, executor = _test_probe(
        "optional",
        ProbeStatus.UNAVAILABLE,
        required=False,
        retryable=True,
    )
    report = _report(preflight_enabled=True, preflight_probes=(probe,), preflight_executor=executor)
    payloads: list[dict[str, Any]] = []

    policy = json.loads(report.to_json())
    policy["policy"]["warning_blocks"] = True
    policy["policy"]["warning_acknowledgment_required"] = True
    payloads.append(policy)

    requirement = json.loads(report.to_json())
    requirement_probe = requirement["stages"][-1]["probe_results"][0]
    requirement_probe["required"] = True
    requirement_probe["fallback_available"] = True
    payloads.append(requirement)

    redaction = json.loads(report.to_json())
    redaction["stages"][-1]["probe_results"][0]["redaction"] = "endpoint_host_omitted"
    payloads.append(redaction)

    owner = json.loads(report.to_json())
    owner_probe = owner["stages"][-1]["probe_results"][0]
    owner_probe["owner"] = "coordinated-owner"
    owner["stages"][-1]["issues"][0]["notes"][1] = "owner=coordinated-owner"
    payloads.append(owner)

    status = json.loads(report.to_json())
    status_probe = status["stages"][-1]["probe_results"][0]
    status_probe["status"] = "degraded"
    status_probe["summary"] = "Probe reports the dependency is degraded."
    status_issue = status["stages"][-1]["issues"][0]
    status_issue["message"] = "Preflight optional: Probe reports the dependency is degraded."
    status_issue["notes"][0] = "status=degraded"
    payloads.append(status)

    blocking = json.loads(report.to_json())
    blocking_probe = blocking["stages"][-1]["probe_results"][0]
    blocking_probe["required"] = True
    blocking_probe["blocking"] = True
    blocking_issue = blocking["stages"][-1]["issues"][0]
    blocking_issue["diagnostic_rule_id"] = "preflight.dependency_unavailable"
    blocking_issue["code"] = code_for_rule("preflight.dependency_unavailable")
    blocking_issue["severity"] = "error"
    blocking_issue["blocking"] = True
    blocking_issue["operational_effect"] = "Environmental readiness is blocked."
    payloads.append(blocking)

    elapsed = json.loads(report.to_json())
    elapsed["stages"][-1]["probe_results"][0]["elapsed_milliseconds"] += 1
    payloads.append(elapsed)

    expected_report = canonical_report_sha256(report.to_dict())
    for payload in payloads:
        _resummarize_report_mapping(payload)
        assert verify_report_mapping(payload, expected_candidate_sha256=report.candidate.sha256).accepted
        verification = _verify_report_mapping(
            payload,
            expected_candidate_sha256=report.candidate.sha256,
            expected_candidate_identity_sha256=report.candidate.identity_sha256,
            expected_report_sha256=expected_report,
        )
        assert VerificationFailure.REPORT_MISMATCH in verification.failures


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("validation_report_version",), True),
        (("validation_report_version",), 1.0),
        (("validator_stamp", "stamp_version"), True),
        (("validator_stamp", "validation_protocol_version"), 1.0),
        (("validator_stamp", "supported_config_schema", "minimum"), True),
        (("validator_stamp", "supported_config_schema", "maximum"), 1.0),
        (("summary", "severity_counts", "deprecation"), True),
        (("summary", "blocking_counts", "nonblocking"), 2.0),
    ],
)
def test_external_report_verifier_requires_exact_integer_json_types(
    path: tuple[str, ...],
    value: object,
) -> None:
    report = _report()
    payload = json.loads(report.to_json())
    selected: dict[str, object] = payload
    for segment in path[:-1]:
        child = selected.get(segment)
        if not isinstance(child, dict):
            raise AssertionError(f"expected mapping at {segment!r}")
        selected = child
    selected[path[-1]] = value

    assert not _verify_report_mapping(
        payload,
        expected_candidate_sha256=report.candidate.sha256,
        expected_candidate_identity_sha256=report.candidate.identity_sha256,
        expected_report_sha256=canonical_report_sha256(payload),
    ).accepted


def test_complete_candidate_identity_rejects_coordinated_manifest_tampering() -> None:
    report = _report()
    payload = json.loads(report.to_json())
    environment = payload["candidate"]["environment_inputs"][0]
    environment["present"] = True
    environment["opaque_change_identity"] = "hmac-sha256:" + "a" * 64
    candidate = payload["candidate"]
    tampered_identity = complete_candidate_sha256(
        source_manifest=candidate["source_manifest"],
        config_schema_version=candidate["config_schema_version"],
        origin_manifest=candidate["origin_manifest"],
        environment_inputs=candidate["environment_inputs"],
    )
    candidate["identity_sha256"] = tampered_identity
    payload["validator_stamp"]["candidate_identity_sha256"] = tampered_identity

    verification = _verify_report_mapping(
        payload,
        expected_candidate_sha256=report.candidate.sha256,
        expected_candidate_identity_sha256=report.candidate.identity_sha256,
        expected_report_sha256=canonical_report_sha256(payload),
    )

    assert VerificationFailure.CANDIDATE_MISMATCH in verification.failures


def test_external_report_verifier_rejects_semantic_downgrade_and_wrong_fixes() -> None:
    semantic_report = _report(
        EXAMPLE.read_text(encoding="utf-8").replace(
            "  total_seconds: 30.0",
            "  total_seconds: 4.0",
            1,
        )
    )
    downgraded = json.loads(semantic_report.to_json())
    stage_issue = downgraded["stages"][2]["issues"][0]
    stage_issue["severity"] = "warning"
    stage_issue["blocking"] = False
    downgraded["issues"][0] = stage_issue

    arbitrary = json.loads(_report().to_json())
    issue = arbitrary["stages"][4]["issues"][0]
    issue["fixes"][0]["operation"] = "replace"
    issue["fixes"][0]["replacement"] = "arbitrary"
    arbitrary["issues"] = [item for stage in arbitrary["stages"] for item in stage["issues"]]

    for payload, report in (
        (downgraded, semantic_report),
        (arbitrary, _report()),
    ):
        assert not _verify_report_mapping(
            payload,
            expected_candidate_sha256=report.candidate.sha256,
            expected_candidate_identity_sha256=report.candidate.identity_sha256,
            expected_report_sha256=canonical_report_sha256(payload),
        ).accepted


def test_external_report_verifier_binds_each_advisory_fix_to_its_rule() -> None:
    report = _report()
    payload = json.loads(report.to_json())
    deprecations = payload["stages"][4]["issues"]
    assert len(deprecations) >= 2
    first_target = deprecations[0]["fixes"][0]["target"]
    deprecations[0]["fixes"][0]["target"] = deprecations[1]["fixes"][0]["target"]
    deprecations[1]["fixes"][0]["target"] = first_target
    payload["issues"] = [issue for stage in payload["stages"] for issue in stage["issues"]]

    assert not _verify_report_mapping(
        payload,
        expected_candidate_sha256=report.candidate.sha256,
        expected_candidate_identity_sha256=report.candidate.identity_sha256,
        expected_report_sha256=canonical_report_sha256(payload),
    ).accepted


def test_external_report_verifier_reconciles_blocking_probe_issue_and_contract_flags() -> None:
    probe, executor = _test_probe(
        "required",
        ProbeStatus.UNAVAILABLE,
        required=True,
        retryable=True,
    )
    report = _report(preflight_enabled=True, preflight_probes=(probe,), preflight_executor=executor)
    omitted = json.loads(report.to_json())
    omitted["stages"][-1]["issues"] = []
    omitted["issues"] = [issue for issue in omitted["issues"] if issue["phase"] != ValidationStage.PREFLIGHT.value]
    omitted["preflight_ready"] = True
    omitted["summary"]["preflight_ready"] = True

    changed_flags = json.loads(report.to_json())
    changed_flags["stages"][-1]["probe_results"][0]["required"] = False
    changed_fallback = json.loads(report.to_json())
    changed_fallback["stages"][-1]["probe_results"][0]["fallback_available"] = True

    for payload in (omitted, changed_flags, changed_fallback):
        assert not _verify_report_mapping(
            payload,
            expected_candidate_sha256=report.candidate.sha256,
            expected_candidate_identity_sha256=report.candidate.identity_sha256,
            expected_report_sha256=canonical_report_sha256(payload),
        ).accepted


def test_external_report_verifier_rejects_unknown_bindings_paths_fixes_and_depth() -> None:
    report = _report()
    expected = report.candidate.sha256
    assert expected is not None
    issue = next(item for item in report.issues if item.fixes)

    unknown = json.loads(report.to_json())
    stage_issue = next(item for stage in unknown["stages"] for item in stage["issues"] if item["code"] == issue.code)
    stage_issue["rule_id"] = "semantic.unknown"

    bad_path = json.loads(report.to_json())
    stage_issue = next(item for stage in bad_path["stages"] for item in stage["issues"] if item.get("fixes"))
    stage_issue["path"]["pointer"] = "/contradictory"

    bad_fix = json.loads(report.to_json())
    stage_issue = next(item for stage in bad_fix["stages"] for item in stage["issues"] if item.get("fixes"))
    stage_issue["fixes"][0]["diagnostic_code"] = "SWCFG0001"

    deep = json.loads(report.to_json())
    nested: object = "leaf"
    for _ in range(20):
        nested = {"next": nested}
    stage_issue = next(item for stage in deep["stages"] for item in stage["issues"] if item.get("fixes"))
    stage_issue["fixes"][0]["expected_old_value"] = nested

    overlong = json.loads(report.to_json())
    overlong["candidate"]["source_manifest"][0]["source"] = "x" * 2_049

    too_many = json.loads(report.to_json())
    too_many["issues"] = [{} for _ in range(257)]

    for payload in (unknown, bad_path, bad_fix, deep, overlong, too_many):
        assert not verify_report_mapping(
            payload,
            expected_candidate_sha256=expected,
        ).accepted


def test_external_report_verifier_binds_probe_identities_results_and_freshness() -> None:
    probe, executor = _test_probe("dependency", ProbeStatus.AVAILABLE, required=True)
    report = _report(
        preflight_enabled=True,
        preflight_probes=(probe,),
        preflight_executor=executor,
        active_configuration_generation=4,
    )
    expected = report.candidate.sha256
    assert expected is not None
    payload = json.loads(report.to_json())

    assert verify_report_mapping(
        payload,
        expected_candidate_sha256=expected,
        current_active_generation=4,
        require_fresh_generation=True,
    ).accepted

    identity = json.loads(report.to_json())
    identity["stages"][-1]["probe_results"][0]["identifier"] = "other"
    impossible = json.loads(report.to_json())
    impossible["stages"][-1]["probe_results"][0]["failure_kind"] = "timeout"
    leaked_summary = json.loads(report.to_json())
    leaked_summary["stages"][-1]["probe_results"][0]["summary"] = "secret endpoint?token=value"
    leaked_evidence = json.loads(report.to_json())
    leaked_evidence["stages"][-1]["probe_results"][0]["evidence"] = ["secret"]
    unknown_redaction = json.loads(report.to_json())
    unknown_redaction["stages"][-1]["probe_results"][0]["redaction"] = "unknown"
    missing_generation = verify_report_mapping(
        payload,
        expected_candidate_sha256=expected,
        require_fresh_generation=True,
    )

    assert not verify_report_mapping(
        identity,
        expected_candidate_sha256=expected,
    ).accepted
    assert not verify_report_mapping(
        impossible,
        expected_candidate_sha256=expected,
    ).accepted
    for malformed in (leaked_summary, leaked_evidence, unknown_redaction):
        assert not verify_report_mapping(
            malformed,
            expected_candidate_sha256=expected,
        ).accepted
    assert VerificationFailure.STALE_GENERATION in missing_generation.failures


def test_validator_stamp_records_all_executed_unique_rules_and_canonical_schema_range() -> None:
    report = _report()
    rules = report.stamp.rule_identities

    assert "semantic.auth.current_legacy" in rules
    assert "semantic.lifecycle.total_covers_stage" in rules
    assert "deprecation.cycle.hwo_max_chars_heightened" in rules
    assert "deprecation.cycle.afd_max_chars_heightened" in rules
    assert len(rules) == len(set(rules))
    assert report.stamp.supported_config_schema.minimum == min(SUPPORTED_CONFIG_SCHEMAS)
    assert report.stamp.supported_config_schema.maximum == max(SUPPORTED_CONFIG_SCHEMAS)


def test_opaque_environment_identity_rejects_raw_text() -> None:
    with pytest.raises(ValueError, match="HMAC"):
        EnvironmentInputIdentity("SEASONAL_API_TOKEN", True, "opaque:raw-secret")


def test_report_candidate_stamp_stage_and_fix_inputs_are_recursively_immutable() -> None:
    report = _report()
    sources = list(report.candidate.source_manifest)
    origins = list(report.candidate.origin_manifest)
    environment = list(report.candidate.environment_inputs)
    rules = list(report.stamp.rule_identities)
    probes = list(report.stamp.probe_identities)
    payload_versions = list(report.stamp.job_payload_schema_versions)
    result_versions = list(report.stamp.job_result_schema_versions)
    stages = list(report.stages)

    candidate = replace(
        report.candidate,
        source_manifest=sources,
        origin_manifest=origins,
        environment_inputs=environment,
    )
    stamp = replace(
        report.stamp,
        rule_identities=rules,
        probe_identities=probes,
        job_payload_schema_versions=payload_versions,
        job_result_schema_versions=result_versions,
    )
    frozen = replace(report, candidate=candidate, stamp=stamp, stages=stages)
    before = frozen.to_json()

    sources.clear()
    origins.clear()
    environment.clear()
    rules.clear()
    probes.clear()
    payload_versions.clear()
    result_versions.clear()
    stages.clear()

    assert frozen.to_json() == before

    lifecycle = _report(
        EXAMPLE.read_text(encoding="utf-8").replace(
            "  total_seconds: 30.0",
            "  total_seconds: 4.0",
            1,
        )
    )
    issue = next(item for item in lifecycle.issues if item.fixes)
    replacement = {"nested": [1]}
    applicability = ["configuration is unchanged"]
    fix = replace(
        issue.fixes[0],
        replacement=replacement,
        applicability=applicability,
    )
    notes = ["stable"]
    fixes = [fix]
    frozen_issue = replace(issue, notes=notes, fixes=fixes)
    serialized = frozen_issue.to_dict()
    replacement["nested"].append(2)
    applicability.append("mutated")
    notes.append("mutated")
    fixes.clear()

    assert frozen_issue.to_dict() == serialized

    segments: list[str | int] = ["items", 0]
    path = DiagnosticPath(PathKind.JSON_POINTER, segments)
    segments.append("mutated")
    assert path.segments == ("items", 0)

    failures: Any = []
    verification = ReportVerification(True, failures)
    failures.append(VerificationFailure.MALFORMED_REPORT)
    assert verification.failures == ()


def test_compatibility_collections_are_defensively_normalized() -> None:
    payload_versions = [1]
    result_versions = [1]
    identity = CompatibilityIdentity(
        software_version="0.17.0",
        build_identity=None,
        validation_protocol_version=1,
        config_schema_version=1,
        swwp_protocol_version=1,
        job_payload_schema_versions=payload_versions,
        job_result_schema_versions=result_versions,
        diagnostic_schema_version=1,
        diagnostic_catalog_version=1,
        capability_manifest_version=1,
        report_schema_version=1,
    )
    payload_supported = {1}
    result_supported = {1}
    supported = replace(
        default_supported_compatibility(),
        job_payload_schemas=payload_supported,
        job_result_schemas=result_supported,
    )

    payload_versions.append(2)
    result_versions.append(2)
    payload_supported.add(2)
    result_supported.add(2)

    assert identity.job_payload_schema_versions == (1,)
    assert identity.job_result_schema_versions == (1,)
    assert supported.job_payload_schemas == frozenset({1})
    assert supported.job_result_schemas == frozenset({1})


def test_warning_policy_is_explicit_and_hard_errors_cannot_be_nonblocking() -> None:
    issue = admission_error(
        json_payload_field("/request/value"),
        message="Request value is invalid.",
        help_text="Supply a supported value.",
    )
    assert issue.blocking
    with pytest.raises(ValueError, match="must block"):
        replace(issue, blocking=False)
    assert ValidationPolicy(warning_blocks=True).warning_blocks


def test_warning_acknowledgment_is_derived_by_the_shared_policy() -> None:
    probe, executor = _test_probe("optional-fixture", ProbeStatus.UNAVAILABLE, required=False)
    report = _report(
        preflight_enabled=True,
        preflight_probes=(probe,),
        preflight_executor=executor,
        policy=ValidationPolicy(warning_acknowledgment_required=True),
    )

    assert report.decision.valid
    assert report.decision.preflight_ready
    assert report.decision.warning_acknowledgment_required
    assert not report.decision.acceptable_for_reload_decision


@pytest.mark.parametrize(
    ("filename", "content_type", "size_bytes", "reason_code", "field", "status_code"),
    [
        ("alert.wav", "audio/wav", 0, "empty_upload", "/data", 422),
        ("alert.wav", "audio/wav", 9, "upload_too_large", "/size_bytes", 413),
        ("alert.mp3", "audio/wav", 1, "unsupported_audio_type", "/filename", 422),
        ("alert.wav", "audio/mpeg", 1, "unsupported_audio_type", "/content_type", 422),
    ],
)
def test_existing_wav_upload_uses_reusable_admission_diagnostics(
    filename: str,
    content_type: str,
    size_bytes: int,
    reason_code: str,
    field: str,
    status_code: int,
) -> None:
    rejection = validate_wav_upload(
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        maximum_bytes=8,
    )

    assert rejection is not None
    assert rejection.reason_code == reason_code
    assert rejection.status_code == status_code
    assert rejection.issue.path is not None
    assert rejection.issue.path.to_pointer() == field
    assert rejection.issue.code == "SWCFG1021"


def test_current_job_auth_and_insert_owners_map_to_typed_admission_paths() -> None:
    with pytest.raises(ValidationError):
        policy_for(JobType.TTS_SYNTHESIZE).payload_schema.model_validate(
            {"synthesis_text": "raw text is not an admitted reference"}
        )
    job_issue = admission_error(
        job_payload_field("payload", "synthesis_text"),
        message="The current job payload policy rejected this field.",
        help_text="Submit the reference-based payload required by the registered job schema.",
    )

    with pytest.raises(AuthenticationError):
        normalize_scopes(("alerts:read", "alerts:read"))
    auth_issue = admission_error(
        authentication_field("scopes", 1),
        message="The current authentication policy rejected this scope.",
        help_text="Submit unique scopes from the supported scope registry.",
    )

    with pytest.raises(ValidationError):
        CreateTextInsertRequest.model_validate(
            {
                "title": "Weather update",
                "text": "Update",
                "start_after": "2026-07-29T12:00:00Z",
                "expires_at": "2026-07-29T11:00:00Z",
            }
        )
    insert_issue = admission_error(
        scheduled_insert_field("expires_at"),
        message="The current scheduled-insert policy rejected this window.",
        help_text="Set expires_at after start_after.",
    )

    assert job_issue.path is not None and job_issue.path.kind.value == "job_payload"
    assert auth_issue.path is not None and auth_issue.path.kind.value == "authentication"
    assert insert_issue.path is not None and insert_issue.path.kind.value == "scheduled_insert"


@pytest.mark.parametrize(
    "field",
    [
        job_payload_field("payload", "text"),
        authentication_field("scopes", 0),
        upload_field("content_type"),
        scheduled_insert_field("expires_at"),
        tts_field("voice"),
        segment_field("key"),
        import_feature_field("counties.csv", "row-7", "geometry"),
        json_payload_field("/items/0/name"),
    ],
)
def test_reusable_admission_paths_are_bounded_and_machine_readable(field: AdmissionField) -> None:
    issue = admission_error(
        field,
        message="The field is invalid.",
        help_text="Correct the identified field.",
    )

    payload = issue.to_dict()
    assert payload["code"] == "SWCFG1021"
    assert payload["path"]["kind"] == field.kind.value
    assert len(payload["path"]["pointer"]) < 512
