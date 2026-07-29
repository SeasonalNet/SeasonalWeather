"""Pure deterministic public catalog representations."""

from __future__ import annotations

from .codes import CLASS_MEANINGS
from .models import DiagnosticCatalog, DiagnosticDefinition, DiagnosticTombstone
from .namespaces import DiagnosticNamespace


def version_representation(catalog: DiagnosticCatalog) -> dict[str, int]:
    return {
        "diagnostic_schema_version": catalog.diagnostic_schema_version,
        "diagnostic_catalog_version": catalog.diagnostic_catalog_version,
    }


def namespace_representation(namespace: DiagnosticNamespace) -> dict[str, object]:
    return {
        "namespace": namespace.token,
        "state": namespace.state.value,
        "scope": namespace.scope,
        "owner": namespace.owner,
        "remediation_domain": namespace.remediation_domain,
    }


def namespace_list_representation(catalog: DiagnosticCatalog) -> dict[str, object]:
    return {
        **version_representation(catalog),
        "namespaces": [namespace_representation(item) for item in catalog.namespaces],
    }


def summary_representation(definition: DiagnosticDefinition) -> dict[str, object]:
    return {
        "code": str(definition.code),
        "title": definition.title,
        "summary": definition.summary,
        "namespace": definition.namespace,
        "subsystem": definition.namespace,
        "condition_class": int(definition.condition_class),
        "condition_class_meaning": CLASS_MEANINGS[definition.condition_class],
        "default_severity": definition.default_severity.value,
        "default_blocking": definition.default_blocking,
        "default_fatal": definition.default_fatal,
        "default_retryable": definition.default_retryable,
        "owner": definition.owner,
        "status": definition.status.value,
    }


def list_representation(catalog: DiagnosticCatalog) -> dict[str, object]:
    return {
        **version_representation(catalog),
        "diagnostics": [summary_representation(item) for item in catalog.definitions],
    }


def detail_representation(
    catalog: DiagnosticCatalog,
    definition: DiagnosticDefinition,
) -> dict[str, object]:
    return {
        **version_representation(catalog),
        "diagnostic": {
            **summary_representation(definition),
            "class_justification": definition.class_justification,
            "introduction_version": definition.introduction_version,
            "explanation_path": definition.explanation_path,
            "related_codes": list(definition.related_codes),
            "documentation_references": list(definition.documentation_references),
            "supported_phases": list(definition.supported_phases),
        },
    }


def explanation_representation(
    catalog: DiagnosticCatalog,
    definition: DiagnosticDefinition,
    markdown: str,
) -> dict[str, object]:
    return {
        **detail_representation(catalog, definition),
        "explanation_markdown": markdown,
    }


def tombstone_representation(
    catalog: DiagnosticCatalog,
    tombstone: DiagnosticTombstone,
) -> dict[str, object]:
    return {
        **version_representation(catalog),
        "diagnostic": {
            "code": str(tombstone.code),
            "status": "retired",
            "original_title": tombstone.original_title,
            "introduction_version": tombstone.introduction_version,
            "retirement_version": tombstone.retirement_version,
            "reason": tombstone.reason,
            "replacement_code": tombstone.replacement_code,
        },
    }


def unknown_representation(catalog: DiagnosticCatalog, code: str) -> dict[str, object]:
    return {
        **version_representation(catalog),
        "error": {
            "kind": "unknown_code",
            "code": code,
            "message": "The diagnostic code is valid but is not assigned in this catalog version.",
        },
    }
