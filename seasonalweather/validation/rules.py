"""Stable validator-rule identities and fail-closed issue contracts."""

from __future__ import annotations

from dataclasses import dataclass

from seasonalweather.diagnostics.models import DiagnosticSeverity

from .issues import StageState, ValidationStage


@dataclass(frozen=True, order=True)
class IssueContract:
    diagnostic_rule_ids: frozenset[str]
    outcomes: frozenset[tuple[str, bool]]
    redacted: bool | None
    fix_contract: str = "forbidden"


@dataclass(frozen=True, order=True)
class ValidatorRule:
    identity: str
    stage: ValidationStage
    issue_contract: IssueContract
    pipeline: bool = True


def _contract(
    diagnostics: str | tuple[str, ...],
    severity: DiagnosticSeverity | tuple[DiagnosticSeverity, ...],
    blocking: bool | tuple[bool, ...],
    *,
    redacted: bool | None = False,
    fix: str = "forbidden",
) -> IssueContract:
    diagnostic_items = (diagnostics,) if isinstance(diagnostics, str) else diagnostics
    severity_items = (severity,) if isinstance(severity, DiagnosticSeverity) else severity
    blocking_items = (blocking,) if isinstance(blocking, bool) else blocking
    return IssueContract(
        frozenset(diagnostic_items),
        frozenset((item.value, block) for item in severity_items for block in blocking_items),
        redacted,
        fix,
    )


_SEMANTIC = _contract("semantic.invariant", DiagnosticSeverity.ERROR, True)
_UNSUPPORTED = _contract("compatibility.unsupported", DiagnosticSeverity.ERROR, True)

RULES = (
    ValidatorRule(
        "compiler.parse", ValidationStage.PARSE, _contract((), DiagnosticSeverity.ERROR, True, redacted=None)
    ),
    ValidatorRule(
        "compiler.schema", ValidationStage.SCHEMA, _contract((), DiagnosticSeverity.ERROR, True, redacted=None)
    ),
    ValidatorRule("semantic.auth.current_legacy", ValidationStage.SEMANTIC, _SEMANTIC),
    ValidatorRule(
        "semantic.auth.static_sources",
        ValidationStage.SEMANTIC,
        _contract("semantic.invariant", DiagnosticSeverity.ERROR, True, redacted=True),
    ),
    ValidatorRule("semantic.auth.exchange_static", ValidationStage.SEMANTIC, _SEMANTIC),
    ValidatorRule("semantic.auth.exchange_ttl_order", ValidationStage.SEMANTIC, _SEMANTIC),
    ValidatorRule("semantic.jobs.required_enabled", ValidationStage.SEMANTIC, _SEMANTIC),
    ValidatorRule("semantic.jobs.path_required", ValidationStage.SEMANTIC, _SEMANTIC),
    ValidatorRule("semantic.jobs.path_distinct", ValidationStage.SEMANTIC, _SEMANTIC),
    ValidatorRule("semantic.jobs.lease_timing", ValidationStage.SEMANTIC, _SEMANTIC),
    ValidatorRule("semantic.lifecycle.positive", ValidationStage.SEMANTIC, _SEMANTIC),
    ValidatorRule("semantic.lifecycle.optional_restart", ValidationStage.SEMANTIC, _SEMANTIC),
    ValidatorRule(
        "semantic.lifecycle.total_covers_stage",
        ValidationStage.SEMANTIC,
        _contract(
            "semantic.invariant",
            DiagnosticSeverity.ERROR,
            True,
            fix="semantic.lifecycle.total_covers_stage",
        ),
    ),
    ValidatorRule(
        "compatibility.identity.software_version",
        ValidationStage.COMPATIBILITY,
        IssueContract(
            frozenset({"compatibility.unsupported", "compatibility.advisory"}),
            frozenset(
                {
                    (DiagnosticSeverity.ERROR.value, True),
                    (DiagnosticSeverity.SUGGESTION.value, False),
                }
            ),
            False,
        ),
    ),
    *(
        ValidatorRule(f"compatibility.identity.{field}", ValidationStage.COMPATIBILITY, _UNSUPPORTED)
        for field in (
            "validation_protocol_version",
            "config_schema_version",
            "swwp_protocol_version",
            "job_payload_schema_versions",
            "job_result_schema_versions",
            "diagnostic_schema_version",
            "diagnostic_catalog_version",
            "capability_manifest_version",
            "report_schema_version",
        )
    ),
    ValidatorRule(
        "compatibility.capability",
        ValidationStage.COMPATIBILITY,
        IssueContract(
            frozenset({"compatibility.unsupported", "compatibility.degraded"}),
            frozenset(
                {
                    (DiagnosticSeverity.ERROR.value, True),
                    (DiagnosticSeverity.WARNING.value, False),
                }
            ),
            False,
        ),
    ),
    ValidatorRule(
        "deprecation.cycle.hwo_max_chars_heightened",
        ValidationStage.DEPRECATION,
        _contract(
            "advisory.deprecated",
            DiagnosticSeverity.DEPRECATION,
            False,
            fix="deprecation.cycle.hwo_max_chars_heightened",
        ),
    ),
    ValidatorRule(
        "deprecation.cycle.afd_max_chars_heightened",
        ValidationStage.DEPRECATION,
        _contract(
            "advisory.deprecated",
            DiagnosticSeverity.DEPRECATION,
            False,
            fix="deprecation.cycle.afd_max_chars_heightened",
        ),
    ),
    ValidatorRule(
        "advisory.tts.espeak_ng_alias",
        ValidationStage.ADVISORY,
        _contract(
            "advisory.configuration",
            DiagnosticSeverity.SUGGESTION,
            False,
            fix="advisory.tts.espeak_ng_alias",
        ),
    ),
    ValidatorRule(
        "preflight.environment",
        ValidationStage.PREFLIGHT,
        IssueContract(
            frozenset(
                {
                    "preflight.degraded",
                    "preflight.dependency_unavailable",
                    "preflight.timeout",
                }
            ),
            frozenset(
                {
                    (DiagnosticSeverity.ERROR.value, True),
                    (DiagnosticSeverity.WARNING.value, False),
                }
            ),
            False,
        ),
    ),
    ValidatorRule(
        "admission.field",
        ValidationStage.SEMANTIC,
        _contract("admission.invalid", DiagnosticSeverity.ERROR, True, redacted=None),
        pipeline=False,
    ),
)

RULE_BY_ID = {rule.identity: rule for rule in RULES}


def expected_rule_identities(
    stage_states: tuple[tuple[ValidationStage, StageState], ...],
) -> tuple[str, ...]:
    """Return every rule executed for the supplied completed stage set."""

    completed = {stage for stage, state in stage_states if state is StageState.COMPLETED}
    return tuple(sorted(rule.identity for rule in RULES if rule.pipeline and rule.stage in completed))


def issue_contract(
    validator_rule_id: str,
    diagnostic_rule_id: str,
    stage: ValidationStage,
) -> IssueContract | None:
    rule = RULE_BY_ID.get(validator_rule_id)
    if rule is None or rule.stage is not stage:
        return None
    if validator_rule_id == "compiler.parse":
        return rule.issue_contract if diagnostic_rule_id.startswith(("source.", "yaml.")) else None
    if validator_rule_id == "compiler.schema":
        return rule.issue_contract if diagnostic_rule_id.startswith(("schema.", "compiler.")) else None
    return rule.issue_contract if diagnostic_rule_id in rule.issue_contract.diagnostic_rule_ids else None


def validate_rule_binding(
    validator_rule_id: str,
    diagnostic_rule_id: str,
    stage: ValidationStage,
) -> bool:
    return issue_contract(validator_rule_id, diagnostic_rule_id, stage) is not None
