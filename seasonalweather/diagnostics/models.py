"""Immutable typed diagnostic catalog models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .codes import ConditionClass, DiagnosticCode
from .namespaces import DiagnosticNamespace

DIAGNOSTIC_SCHEMA_VERSION = 1
DIAGNOSTIC_CATALOG_VERSION = 1


class DiagnosticSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    DEPRECATION = "deprecation"
    SUGGESTION = "suggestion"
    INFO = "info"


class DefinitionStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True)
class DiagnosticDefinition:
    code: DiagnosticCode
    title: str
    summary: str
    namespace: str
    condition_class: ConditionClass
    class_justification: str
    default_severity: DiagnosticSeverity
    default_blocking: bool
    default_fatal: bool
    default_retryable: bool
    owner: str
    introduction_version: str
    status: DefinitionStatus
    explanation_path: str
    related_codes: tuple[str, ...]
    documentation_references: tuple[str, ...]
    supported_phases: tuple[str, ...]


@dataclass(frozen=True)
class DiagnosticTombstone:
    code: DiagnosticCode
    original_title: str
    introduction_version: str
    retirement_version: str
    reason: str
    replacement_code: str | None


@dataclass(frozen=True)
class DiagnosticCatalog:
    diagnostic_schema_version: int
    diagnostic_catalog_version: int
    namespaces: tuple[DiagnosticNamespace, ...]
    definitions: tuple[DiagnosticDefinition, ...]
    tombstones: tuple[DiagnosticTombstone, ...]

    def definition(self, code: str) -> DiagnosticDefinition | None:
        return next((item for item in self.definitions if str(item.code) == code), None)

    def tombstone(self, code: str) -> DiagnosticTombstone | None:
        return next((item for item in self.tombstones if str(item.code) == code), None)
