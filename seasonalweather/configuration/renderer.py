"""Deterministic compiler-style human rendering."""

from __future__ import annotations

from collections.abc import Iterable

from .issues import CompileIssue
from .redaction import (
    is_secret_path,
    line_looks_secret,
    redact_source_line,
    secret_source_lines,
)
from .report import CompileReport
from .source import DEFAULT_LIMITS, CompilerLimits, SourceDocument, SourceLocation


def render_report(
    report: CompileReport,
    *,
    sources: Iterable[SourceDocument] = (),
    limits: CompilerLimits = DEFAULT_LIMITS,
) -> str:
    registry = {source.source_id: source for source in sources}
    if not report.issues:
        return "configuration parse/schema validation succeeded"
    return "\n\n".join(_render_issue(issue, registry=registry, limits=limits) for issue in report.issues)


def _render_issue(
    issue: CompileIssue,
    *,
    registry: dict[str, SourceDocument],
    limits: CompilerLimits,
) -> str:
    lines = [f"{issue.severity}[{issue.rule_id}]: {issue.message}"]
    if issue.path is not None:
        lines.append(f"  path: {issue.path.to_human()}")
    if issue.primary is not None:
        position = issue.primary.span.start
        lines.append(f"  --> {issue.primary.source_id}:{position.line + 1}:{position.column + 1}")
        document = registry.get(issue.primary.source_id)
        if document is not None:
            lines.extend(
                _frame(
                    document,
                    issue.primary,
                    redacted=issue.redacted or is_secret_path(issue.path),
                    limits=limits,
                )
            )
    for related in issue.related[: limits.max_related_locations]:
        location = related.location
        position = location.span.start
        lines.append(
            f"  = related ({related.relationship}): {location.source_id}:{position.line + 1}:{position.column + 1}"
        )
    for note in issue.notes:
        lines.append(f"  = note: {note}")
    if issue.help:
        lines.append(f"  = help: {issue.help}")
    return "\n".join(lines)


def _frame(
    document: SourceDocument,
    location: SourceLocation,
    *,
    redacted: bool,
    limits: CompilerLimits,
) -> list[str]:
    source_lines = document.lines()
    if not source_lines:
        return []
    start = min(location.span.start.line, len(source_lines) - 1)
    end = min(max(start, location.span.end.line), len(source_lines) - 1)
    first = max(0, start - limits.frame_context_lines)
    last = min(len(source_lines) - 1, end + limits.frame_context_lines)
    width = len(str(last + 1))
    output = ["   |"]
    secret_lines = secret_source_lines(source_lines)
    for line_index in range(first, last + 1):
        original = source_lines[line_index]
        hide = redacted or line_looks_secret(original) or line_index in secret_lines
        rendered = redact_source_line(original) if hide else original
        rendered = _bounded_line(rendered, limits.frame_line_width)
        output.append(f"{line_index + 1:>{width}} | {rendered}")
        if start <= line_index <= end:
            output.append(
                _underline(
                    location,
                    line_index=line_index,
                    start=start,
                    rendered=rendered,
                    hidden=hide,
                    width=width,
                )
            )
    output.append("   |")
    return output


def _underline(
    location: SourceLocation,
    *,
    line_index: int,
    start: int,
    rendered: str,
    hidden: bool,
    width: int,
) -> str:
    underline_start = min(location.span.start.column, len(rendered)) if line_index == start else 0
    underline_end = (
        min(location.span.end.column, len(rendered)) if line_index == location.span.end.line else len(rendered)
    )
    if hidden:
        underline_start = min(underline_start, len(rendered))
        underline_end = len(rendered)
    length = max(1, underline_end - underline_start)
    marker = "^" if line_index == start else "-"
    return f"{'':>{width}} | {' ' * underline_start}{marker * length}"


def _bounded_line(line: str, width: int) -> str:
    if len(line) <= width:
        return line
    return f"{line[: max(0, width - 1)]}…"
