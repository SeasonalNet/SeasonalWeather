from __future__ import annotations

import pytest

from seasonalweather.diagnostics.bindings import binding_for_rule
from seasonalweather.diagnostics.codes import ConditionClass
from seasonalweather.diagnostics.loader import load_catalog, load_explanation
from seasonalweather.diagnostics.models import DiagnosticSeverity


@pytest.mark.parametrize(
    ("rule_id", "code", "condition_class", "severity", "fatal"),
    [
        (
            "facts.insufficient_history",
            "SWFACT4001",
            ConditionClass.DEGRADATION,
            DiagnosticSeverity.WARNING,
            False,
        ),
        (
            "facts.invariant_violation",
            "SWFACT5001",
            ConditionClass.PERMANENT_FAILURE,
            DiagnosticSeverity.ERROR,
            True,
        ),
    ],
)
def test_fact_binding_uses_governed_catalog_policy(
    rule_id: str,
    code: str,
    condition_class: ConditionClass,
    severity: DiagnosticSeverity,
    fatal: bool,
) -> None:
    binding = binding_for_rule(rule_id)
    assert binding.code == code
    assert binding.phase == "runtime"

    definition = load_catalog().definition(code)
    assert definition is not None
    assert definition.namespace == "SWFACT"
    assert definition.condition_class is condition_class
    assert definition.default_severity is severity
    assert definition.default_blocking is fatal
    assert definition.default_fatal is fatal
    assert definition.default_retryable is not fatal
    assert load_explanation(definition.explanation_path).startswith(f"# {code} — ")


def test_fact_bindings_do_not_claim_no_change_or_upstream_failures() -> None:
    with pytest.raises(ValueError, match="unmapped diagnostic rule ID"):
        binding_for_rule("facts.no_change")
    assert binding_for_rule("cap.source_failed").code == "SWCAP3001"
    assert binding_for_rule("database.operation_failed").code == "SWDB3001"
    assert binding_for_rule("geography.policy_rejected").code == "SWGEO2001"
