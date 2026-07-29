"""Bounded parse/schema issue model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from seasonalweather.diagnostics.bindings import code_for_rule

from .origins import ValueOrigin
from .paths import ConfigPath
from .source import RelatedLocation, SourceLocation


class IssuePhase(StrEnum):
    PARSE = "parse"
    SCHEMA = "schema"


@dataclass(frozen=True)
class CompileIssue:
    rule_id: str
    phase: IssuePhase
    message: str
    path: ConfigPath | None = None
    primary: SourceLocation | None = None
    related: tuple[RelatedLocation, ...] = ()
    notes: tuple[str, ...] = ()
    help: str | None = None
    origin: ValueOrigin | None = None
    redacted: bool = False
    severity: str = "error"
    blocking: bool = True

    @property
    def code(self) -> str:
        return code_for_rule(self.rule_id)

    def sort_key(self) -> tuple[object, ...]:
        position = self.primary.span.start if self.primary else None
        return (
            0 if self.phase is IssuePhase.PARSE else 1,
            self.primary.source_id if self.primary else "",
            position.line if position else 2**31,
            position.column if position else 2**31,
            self.path or ConfigPath(),
            self.rule_id,
            self.message,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "rule_id": self.rule_id,
            "phase": self.phase.value,
            "severity": self.severity,
            "blocking": self.blocking,
            "message": self.message,
            "redacted": self.redacted,
        }
        if self.path is not None:
            result["path"] = {
                "pointer": self.path.to_pointer(),
                "human": self.path.to_human(),
            }
        if self.primary:
            result["primary_location"] = self.primary.to_dict()
        if self.related:
            result["related_locations"] = [location.to_dict() for location in self.related]
        if self.notes:
            result["notes"] = list(self.notes)
        if self.help:
            result["help"] = self.help
        if self.origin:
            result["origin"] = self.origin.to_dict()
        return result
