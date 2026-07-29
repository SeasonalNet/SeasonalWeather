"""Bounded deterministic human catalog rendering."""

from __future__ import annotations

from .codes import CLASS_MEANINGS
from .models import DiagnosticCatalog
from .namespaces import NamespaceState
from .registry import ExplanationResult


def render_list(catalog: DiagnosticCatalog) -> str:
    lines = [
        (
            "SeasonalWeather diagnostic catalog "
            f"{catalog.diagnostic_catalog_version} "
            f"(schema {catalog.diagnostic_schema_version})"
        )
    ]
    for item in catalog.definitions:
        lines.append(f"{item.code}  {item.default_severity.value:<11}  {item.title}")
    return "\n".join(lines)


def render_namespaces(catalog: DiagnosticCatalog) -> str:
    lines = [
        (
            "SeasonalWeather diagnostic namespaces "
            f"(catalog {catalog.diagnostic_catalog_version}, schema {catalog.diagnostic_schema_version})"
        )
    ]
    for item in catalog.namespaces:
        marker = "reserved" if item.state is NamespaceState.RESERVED else "active"
        lines.append(f"{item.token}  {marker:<8}  {item.scope}")
    return "\n".join(lines)


def render_explanation(catalog: DiagnosticCatalog, result: ExplanationResult) -> str:
    item = result.definition
    metadata = [
        f"Code: {item.code}",
        f"Title: {item.title}",
        f"Catalog: {catalog.diagnostic_catalog_version}",
        f"Diagnostic schema: {catalog.diagnostic_schema_version}",
        f"Namespace: {item.namespace}",
        f"Condition class: {int(item.condition_class)}xxx — {CLASS_MEANINGS[item.condition_class]}",
        f"Default severity: {item.default_severity.value}",
        f"Blocking: {str(item.default_blocking).lower()}",
        f"Fatal: {str(item.default_fatal).lower()}",
        f"Retryable: {str(item.default_retryable).lower()}",
        f"Owner: {item.owner}",
        "",
        result.markdown.rstrip(),
    ]
    return "\n".join(metadata)
