"""Immutable package-resource loading for the compiled catalog."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path

from .codes import ConditionClass, DiagnosticCode, DiagnosticCodeError
from .models import (
    DIAGNOSTIC_CATALOG_VERSION,
    DIAGNOSTIC_SCHEMA_VERSION,
    DefinitionStatus,
    DiagnosticCatalog,
    DiagnosticDefinition,
    DiagnosticSeverity,
    DiagnosticTombstone,
)
from .namespaces import NAMESPACES

MAX_COMPILED_BYTES = 1_048_576
MAX_EXPLANATION_BYTES = 131_072
_DEFINITION_FIELDS = {
    "code",
    "title",
    "summary",
    "namespace",
    "condition_class",
    "class_justification",
    "default_severity",
    "default_blocking",
    "default_fatal",
    "default_retryable",
    "owner",
    "introduction_version",
    "status",
    "explanation_path",
    "related_codes",
    "documentation_references",
    "supported_phases",
}
_TOMBSTONE_FIELDS = {
    "code",
    "original_title",
    "introduction_version",
    "retirement_version",
    "reason",
    "replacement_code",
}


class CatalogLoadError(RuntimeError):
    """Bounded packaged-resource failure."""


@lru_cache(maxsize=1)
def load_catalog() -> DiagnosticCatalog:
    try:
        resource = resources.files("seasonalweather.diagnostics").joinpath("catalog/catalog.json")
        data = resource.read_bytes()
        if len(data) > MAX_COMPILED_BYTES:
            raise ValueError("compiled catalog exceeds resource bound")
        raw = json.loads(data)
        return _from_compiled(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise CatalogLoadError("Packaged diagnostic catalog is missing or invalid.") from exc


def load_explanation(path: str) -> str:
    try:
        _validate_resource_explanation_path(path)
        resource = resources.files("seasonalweather.diagnostics").joinpath("catalog", *path.split("/"))
        data = resource.read_bytes()
        if len(data) > MAX_EXPLANATION_BYTES:
            raise ValueError("packaged explanation exceeds resource bound")
        return data.decode("utf-8", errors="strict")
    except (OSError, UnicodeError, ValueError) as exc:
        raise CatalogLoadError("Packaged diagnostic explanation is missing or invalid.") from exc


def packaged_catalog_bytes() -> bytes:
    try:
        data = resources.files("seasonalweather.diagnostics").joinpath("catalog/catalog.json").read_bytes()
        if len(data) > MAX_COMPILED_BYTES:
            raise ValueError("compiled catalog exceeds resource bound")
        return data
    except (OSError, ValueError) as exc:
        raise CatalogLoadError("Packaged diagnostic catalog is missing or invalid.") from exc


def packaged_explanation_bytes(path: str) -> bytes:
    try:
        _validate_resource_explanation_path(path)
        data = resources.files("seasonalweather.diagnostics").joinpath("catalog", *path.split("/")).read_bytes()
        if len(data) > MAX_EXPLANATION_BYTES:
            raise ValueError("packaged explanation exceeds resource bound")
        return data
    except (OSError, ValueError) as exc:
        raise CatalogLoadError("Packaged diagnostic explanation is missing or invalid.") from exc


def _from_compiled(raw: object) -> DiagnosticCatalog:
    if not isinstance(raw, dict):
        raise ValueError("compiled catalog is not an object")
    if set(raw) != {
        "diagnostic_schema_version",
        "diagnostic_catalog_version",
        "namespaces",
        "definitions",
        "tombstones",
    }:
        raise ValueError("compiled catalog fields are invalid")
    if raw["diagnostic_schema_version"] != DIAGNOSTIC_SCHEMA_VERSION:
        raise ValueError("unsupported diagnostic schema version")
    if raw["diagnostic_catalog_version"] != DIAGNOSTIC_CATALOG_VERSION:
        raise ValueError("unsupported diagnostic catalog version")
    if raw["namespaces"] != _namespace_dicts():
        raise ValueError("compiled namespace registry contradicts code registry")
    definitions = tuple(_definition(item) for item in _array(raw["definitions"], "definitions"))
    tombstones = tuple(_tombstone(item) for item in _array(raw["tombstones"], "tombstones"))
    _validate_compiled_order_and_identity(definitions, tombstones)
    _validate_compiled_relationships(definitions, tombstones)
    return DiagnosticCatalog(
        diagnostic_schema_version=DIAGNOSTIC_SCHEMA_VERSION,
        diagnostic_catalog_version=DIAGNOSTIC_CATALOG_VERSION,
        namespaces=NAMESPACES,
        definitions=definitions,
        tombstones=tombstones,
    )


def _validate_compiled_order_and_identity(
    definitions: tuple[DiagnosticDefinition, ...],
    tombstones: tuple[DiagnosticTombstone, ...],
) -> None:
    if tuple(sorted(definitions, key=lambda item: item.code)) != definitions:
        raise ValueError("compiled definitions are not canonical")
    if len({str(item.code) for item in definitions}) != len(definitions):
        raise ValueError("compiled definitions contain duplicate codes")
    if tuple(sorted(tombstones, key=lambda item: item.code)) != tombstones:
        raise ValueError("compiled tombstones are not canonical")
    if len({str(item.code) for item in tombstones}) != len(tombstones):
        raise ValueError("compiled tombstones contain duplicate codes")
    active_codes = {str(item.code) for item in definitions}
    retired_codes = {str(item.code) for item in tombstones}
    if active_codes & retired_codes:
        raise ValueError("compiled active code reuses tombstone")


def _validate_compiled_relationships(
    definitions: tuple[DiagnosticDefinition, ...],
    tombstones: tuple[DiagnosticTombstone, ...],
) -> None:
    active_codes = {str(item.code) for item in definitions}
    known_codes = active_codes | {str(item.code) for item in tombstones}
    for definition in definitions:
        if any(related not in known_codes for related in definition.related_codes):
            raise ValueError("compiled definition has broken related code")
    for tombstone in tombstones:
        if tombstone.replacement_code is not None and tombstone.replacement_code not in active_codes:
            raise ValueError("compiled tombstone has broken replacement")


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} is not an array")
    return value


def _definition(raw: object) -> DiagnosticDefinition:
    if not isinstance(raw, dict):
        raise ValueError("definition is not an object")
    if set(raw) != _DEFINITION_FIELDS:
        raise ValueError("definition fields are invalid")
    code = DiagnosticCode.parse(_string(raw, "code"))
    namespace = _string(raw, "namespace")
    condition_class = ConditionClass(_integer(raw, "condition_class"))
    definition = DiagnosticDefinition(
        code=code,
        title=_string(raw, "title"),
        summary=_string(raw, "summary"),
        namespace=namespace,
        condition_class=condition_class,
        class_justification=_string(raw, "class_justification"),
        default_severity=DiagnosticSeverity(_string(raw, "default_severity")),
        default_blocking=_boolean(raw, "default_blocking"),
        default_fatal=_boolean(raw, "default_fatal"),
        default_retryable=_boolean(raw, "default_retryable"),
        owner=_string(raw, "owner"),
        introduction_version=_string(raw, "introduction_version"),
        status=DefinitionStatus(_string(raw, "status")),
        explanation_path=_string(raw, "explanation_path"),
        related_codes=_strings(raw, "related_codes"),
        documentation_references=_strings(raw, "documentation_references"),
        supported_phases=_strings(raw, "supported_phases"),
    )
    if definition.namespace != code.namespace or definition.condition_class is not code.condition_class:
        raise ValueError("compiled definition contradicts code")
    if definition.status is not DefinitionStatus.ACTIVE:
        raise ValueError("compiled definition is not active")
    if definition.explanation_path != f"explanations/{code}.md":
        raise ValueError("compiled explanation path contradicts code")
    if not definition.supported_phases:
        raise ValueError("compiled definition has no phase")
    if tuple(sorted(definition.related_codes)) != definition.related_codes:
        raise ValueError("compiled related codes are not canonical")
    if len(set(definition.related_codes)) != len(definition.related_codes):
        raise ValueError("compiled related codes contain duplicates")
    return definition


def _tombstone(raw: object) -> DiagnosticTombstone:
    if not isinstance(raw, dict):
        raise ValueError("tombstone is not an object")
    if set(raw) != _TOMBSTONE_FIELDS:
        raise ValueError("tombstone fields are invalid")
    replacement = raw.get("replacement_code")
    if replacement is not None and not isinstance(replacement, str):
        raise ValueError("invalid replacement code")
    try:
        code = DiagnosticCode.parse(_string(raw, "code"))
    except DiagnosticCodeError as exc:
        raise ValueError("invalid tombstone code") from exc
    return DiagnosticTombstone(
        code=code,
        original_title=_string(raw, "original_title"),
        introduction_version=_string(raw, "introduction_version"),
        retirement_version=_string(raw, "retirement_version"),
        reason=_string(raw, "reason"),
        replacement_code=replacement,
    )


def _string(raw: dict[object, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {name}")
    return value


def _integer(raw: dict[object, object], name: str) -> int:
    value = raw.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"invalid {name}")
    return value


def _boolean(raw: dict[object, object], name: str) -> bool:
    value = raw.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"invalid {name}")
    return value


def _strings(raw: dict[object, object], name: str) -> tuple[str, ...]:
    value = raw.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"invalid {name}")
    return tuple(value)


def _namespace_dicts() -> list[dict[str, str]]:
    return [
        {
            "token": item.token,
            "state": item.state.value,
            "scope": item.scope,
            "owner": item.owner,
            "remediation_domain": item.remediation_domain,
        }
        for item in NAMESPACES
    ]


def _validate_resource_explanation_path(path: str) -> None:
    if path.split("/") != ["explanations", Path(path).name] or Path(path).name in {"", ".", ".."}:
        raise ValueError("invalid packaged explanation path")
