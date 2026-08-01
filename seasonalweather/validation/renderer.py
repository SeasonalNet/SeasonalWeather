"""Deterministic compiler-style rendering for aggregate validation reports."""

from __future__ import annotations

from collections.abc import Iterable

from seasonalweather.configuration.paths import ConfigPath
from seasonalweather.configuration.redaction import is_secret_path
from seasonalweather.configuration.renderer import _frame
from seasonalweather.configuration.source import DEFAULT_LIMITS, CompilerLimits, SourceDocument

from .issues import ValidationIssue
from .paths import PathKind
from .pipeline import ValidationReport


def render_validation_report(
    report: ValidationReport,
    *,
    sources: Iterable[SourceDocument] = (),
    limits: CompilerLimits = DEFAULT_LIMITS,
) -> str:
    registry = {source.source_id: source for source in sources}
    sections = [_render_issue(issue, registry=registry, limits=limits) for issue in report.issues]
    for stage in report.stages:
        if stage.skipped_reason:
            sections.append(f"info[{stage.stage.value}]: skipped — {stage.skipped_reason}")
    return "\n\n".join(sections) if sections else "configuration validation succeeded"


def _render_issue(
    issue: ValidationIssue,
    *,
    registry: dict[str, SourceDocument],
    limits: CompilerLimits,
) -> str:
    lines = [f"{issue.severity.value}[{issue.code}]: {issue.message}"]
    if issue.path:
        lines.append(f"  path ({issue.path.kind.value}): {issue.path.to_human()}")
    lines.extend(_primary_lines(issue, registry=registry, limits=limits))
    lines.extend(_evidence_lines(issue))
    lines.extend(
        (
            "",
            "For more information, run:",
            f"  seasonalweather diagnostics explain {issue.code}",
        )
    )
    return "\n".join(lines)


def _primary_lines(
    issue: ValidationIssue,
    *,
    registry: dict[str, SourceDocument],
    limits: CompilerLimits,
) -> list[str]:
    if issue.primary is None:
        return []
    position = issue.primary.span.start
    lines = [f"  --> {issue.primary.source_id}:{position.line + 1}:{position.column + 1}"]
    document = registry.get(issue.primary.source_id)
    if document is None:
        return lines
    configuration_path = issue.path if issue.path and issue.path.kind is PathKind.CONFIGURATION else None
    secret = is_secret_path(ConfigPath(configuration_path.segments)) if configuration_path else False
    lines.extend(
        _frame(
            document,
            issue.primary,
            redacted=issue.redacted or secret,
            limits=limits,
        )
    )
    return lines


def _evidence_lines(issue: ValidationIssue) -> list[str]:
    lines: list[str] = []
    for related in issue.related:
        position = related.location.span.start
        lines.append(
            f"  = related ({related.relationship}): "
            f"{related.location.source_id}:{position.line + 1}:{position.column + 1}"
        )
    lines.extend(f"  = note: {note}" for note in issue.notes)
    optional = (
        ("effect", issue.operational_effect),
        ("help", issue.help),
        ("fixes", f"{len(issue.fixes)} fenced machine-readable fix(es)" if issue.fixes else None),
    )
    lines.extend(f"  = {label}: {value}" for label, value in optional if value is not None)
    return lines
