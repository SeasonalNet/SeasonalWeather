"""Read-only application service for catalog lookup and explanation."""

from __future__ import annotations

from dataclasses import dataclass

from .codes import DiagnosticCode, DiagnosticCodeError
from .loader import load_catalog, load_explanation
from .models import DiagnosticCatalog, DiagnosticDefinition, DiagnosticTombstone


class CatalogLookupError(LookupError):
    def __init__(self, kind: str, code: str, message: str) -> None:
        self.kind = kind
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ExplanationResult:
    definition: DiagnosticDefinition
    markdown: str


class DiagnosticCatalogService:
    def __init__(self, catalog: DiagnosticCatalog | None = None) -> None:
        self._catalog = catalog or load_catalog()

    @property
    def catalog(self) -> DiagnosticCatalog:
        return self._catalog

    def lookup(self, code: str) -> DiagnosticDefinition:
        try:
            parsed = DiagnosticCode.parse(code)
        except DiagnosticCodeError as exc:
            raise CatalogLookupError(exc.kind, code, str(exc)) from exc
        canonical = str(parsed)
        definition = self._catalog.definition(canonical)
        if definition is not None:
            return definition
        tombstone = self._catalog.tombstone(canonical)
        if tombstone is not None:
            raise CatalogLookupError("retired", canonical, "Diagnostic code is retired.")
        raise CatalogLookupError(
            "unknown",
            canonical,
            "Diagnostic code is valid but is not assigned in this catalog version.",
        )

    def tombstone(self, code: str) -> DiagnosticTombstone | None:
        return self._catalog.tombstone(code)

    def explain(self, code: str) -> ExplanationResult:
        definition = self.lookup(code)
        return ExplanationResult(
            definition=definition,
            markdown=load_explanation(definition.explanation_path),
        )
