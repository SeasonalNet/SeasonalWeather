"""Deterministic machine-readable compiler reports."""

from __future__ import annotations

import json
from dataclasses import dataclass

from seasonalweather.diagnostics.models import (
    DIAGNOSTIC_CATALOG_VERSION,
    DIAGNOSTIC_SCHEMA_VERSION,
)

from .issues import CompileIssue
from .origins import OriginKind, ValueOrigin

COMPILER_REPORT_VERSION = 2


@dataclass(frozen=True)
class SourceSummary:
    source_id: str
    sha256: str | None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"source": self.source_id}
        if self.sha256:
            result["sha256"] = self.sha256
        return result


@dataclass(frozen=True)
class CompileReport:
    parse_valid: bool
    schema_valid: bool
    explicit_config_schema: int | None
    resolved_config_schema: int | None
    sources: tuple[SourceSummary, ...]
    issues: tuple[CompileIssue, ...]
    origins: tuple[ValueOrigin, ...] = ()

    @property
    def valid(self) -> bool:
        return self.parse_valid and self.schema_valid

    def to_dict(self) -> dict[str, object]:
        counts = {kind.value: sum(1 for origin in self.origins if origin.kind is kind) for kind in OriginKind}
        return {
            "compiler_report_version": COMPILER_REPORT_VERSION,
            "diagnostic_schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "diagnostic_catalog_version": DIAGNOSTIC_CATALOG_VERSION,
            "valid": self.valid,
            "parse_valid": self.parse_valid,
            "schema_valid": self.schema_valid,
            "explicit_config_schema": self.explicit_config_schema,
            "resolved_config_schema": self.resolved_config_schema,
            "sources": [source.to_dict() for source in self.sources],
            "issues": [issue.to_dict() for issue in self.issues],
            "origin_counts": counts,
            "redaction_occurred": any(issue.redacted for issue in self.issues),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
