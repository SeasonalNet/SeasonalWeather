"""Versioned deterministic deprecation and advisory registry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from seasonalweather.configuration.compiler import CompiledConfiguration
from seasonalweather.configuration.paths import ConfigPath
from seasonalweather.configuration.source import ParsedSource, SourceLocation
from seasonalweather.diagnostics.bindings import code_for_rule
from seasonalweather.diagnostics.models import DiagnosticSeverity

from .issues import FixOperation, FixSafety, MachineFix, ValidationIssue, ValidationStage
from .paths import DiagnosticPath


@dataclass(frozen=True)
class AdvisoryRule:
    validator_rule_id: str
    diagnostic_rule_id: str
    path: ConfigPath
    severity: DiagnosticSeverity
    message: str
    help: str
    owner: str
    introduced: str
    removal_condition: str
    replacement: object | None = None
    remove: bool = False


RULES = (
    AdvisoryRule(
        "deprecation.cycle.hwo_max_chars_heightened",
        "advisory.deprecated",
        ConfigPath(("cycle", "hwo", "max_chars_heightened")),
        DiagnosticSeverity.DEPRECATION,
        "Heightened-mode HWO truncation is supported but ignored.",
        "Remove this setting; heightened mode postpones routine segments.",
        "broadcast-cycle",
        "0.18.0",
        "Remove when configuration schema 1 compatibility is retired.",
        remove=True,
    ),
    AdvisoryRule(
        "deprecation.cycle.afd_max_chars_heightened",
        "advisory.deprecated",
        ConfigPath(("cycle", "afd", "max_chars_heightened")),
        DiagnosticSeverity.DEPRECATION,
        "Heightened-mode AFD truncation is supported but ignored.",
        "Remove this setting; heightened mode postpones routine segments.",
        "broadcast-cycle",
        "0.18.0",
        "Remove when configuration schema 1 compatibility is retired.",
        remove=True,
    ),
    AdvisoryRule(
        "advisory.tts.espeak_ng_alias",
        "advisory.configuration",
        ConfigPath(("tts", "backend")),
        DiagnosticSeverity.SUGGESTION,
        "The espeak_ng alias is supported; espeak-ng is the canonical spelling.",
        "Replace espeak_ng with espeak-ng.",
        "tts",
        "0.18.0",
        "Remove when the underscore compatibility alias is retired.",
        replacement="espeak-ng",
    ),
)


def _lookup(root: Mapping[str, object], path: ConfigPath) -> tuple[bool, object | None]:
    current: object = root
    for segment in path.segments:
        if not isinstance(segment, str) or not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def evaluate_advisories(compiled: CompiledConfiguration) -> tuple[ValidationIssue, ...]:
    if not compiled.valid or compiled.value is None or compiled.parsed is None:
        return ()
    output = [
        issue
        for rule in RULES
        if (
            issue := _evaluate_rule(
                compiled,
                rule,
                value=compiled.value,
                parsed=compiled.parsed,
            )
        )
        is not None
    ]
    return tuple(sorted(output, key=ValidationIssue.sort_key))


def _evaluate_rule(
    compiled: CompiledConfiguration,
    rule: AdvisoryRule,
    *,
    value: Mapping[str, object],
    parsed: ParsedSource,
) -> ValidationIssue | None:
    present, old_value = _lookup(value, rule.path)
    if not present:
        return None
    if rule.path == ConfigPath(("tts", "backend")) and old_value != "espeak_ng":
        return None
    node = parsed.locations.get(rule.path)
    location = node.value if node else None
    fix = _rule_fix(compiled, rule, old_value=old_value, location=location)
    phase = ValidationStage.DEPRECATION if rule.severity is DiagnosticSeverity.DEPRECATION else ValidationStage.ADVISORY
    return ValidationIssue(
        rule_id=rule.diagnostic_rule_id,
        validator_rule_id=rule.validator_rule_id,
        phase=phase,
        severity=rule.severity,
        blocking=False,
        message=rule.message,
        path=DiagnosticPath.configuration(rule.path),
        primary=location,
        notes=(
            f"owner={rule.owner}",
            f"rule_introduced={rule.introduced}",
            f"removal_condition={rule.removal_condition}",
        ),
        help=rule.help,
        fixes=(fix,),
        documentation_reference="docs/configuration-validation.md",
    )


def _rule_fix(
    compiled: CompiledConfiguration,
    rule: AdvisoryRule,
    *,
    old_value: object,
    location: SourceLocation | None,
) -> MachineFix:
    return MachineFix(
        operation=FixOperation.REMOVE if rule.remove else FixOperation.REPLACE,
        target=DiagnosticPath.configuration(rule.path),
        diagnostic_code=code_for_rule(rule.diagnostic_rule_id),
        safety=FixSafety.SAFE,
        replacement=None if rule.remove else rule.replacement,
        expected_old_value=old_value,
        expected_source_sha256=compiled.source.digest if compiled.source else None,
        applicability=(rule.removal_condition,),
        location=location,
    )
