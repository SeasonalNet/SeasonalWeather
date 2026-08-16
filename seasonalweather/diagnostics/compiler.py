"""Deterministic offline diagnostic catalog compiler."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from .bindings import NWWS_CODES, RELOAD_CODES, RULE_BINDINGS, RUNTIME_CODES, SEGMENT_BINDINGS, RuleCodeBinding
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
from .namespaces import NAMESPACE_BY_TOKEN, NAMESPACES, NamespaceState

CATALOG_ROOT = Path(__file__).with_name("catalog")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = CATALOG_ROOT / "source.json"
GENERATED_PATH = CATALOG_ROOT / "catalog.json"
MAX_SOURCE_BYTES = 1_048_576
MAX_EXPLANATION_BYTES = 131_072
REQUIRED_EXPLANATION_HEADINGS = (
    "## Meaning",
    "## Trigger",
    "## Correction or recovery",
    "## Operational effect",
    "## Rationale",
    "## Alternatives or migration",
    "## Related diagnostics",
)
_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_TOP_FIELDS = {
    "diagnostic_schema_version",
    "diagnostic_catalog_version",
    "allocation_ceilings",
    "definitions",
    "tombstones",
}
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


class CatalogCompileError(ValueError):
    """Bounded deterministic catalog-source failure."""


class _DuplicateKeyError(ValueError):
    pass


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate metadata key: {key}")
        result[key] = value
    return result


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise CatalogCompileError(f"catalog source could not be read: {path.name}") from exc
    if len(data) > limit:
        raise CatalogCompileError(f"catalog source exceeds byte limit: {path.name}")
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CatalogCompileError(f"catalog source is not UTF-8: {path.name}") from exc
    return data


def _load_json(path: Path) -> dict[str, object]:
    data = _read_bounded(path, MAX_SOURCE_BYTES)
    try:
        value = json.loads(data, object_pairs_hook=_object_pairs)
    except (_DuplicateKeyError, json.JSONDecodeError) as exc:
        raise CatalogCompileError(f"invalid catalog JSON: {exc}") from exc
    return _expect_object(value, _TOP_FIELDS, "catalog")


def _expect_object(value: object, fields: set[str], context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CatalogCompileError(f"{context} must be an object")
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown:
        raise CatalogCompileError(f"{context} has unknown field: {unknown[0]}")
    if missing:
        raise CatalogCompileError(f"{context} is missing field: {missing[0]}")
    if any(not isinstance(key, str) for key in value):
        raise CatalogCompileError(f"{context} keys must be strings")
    return value


def _expect_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogCompileError(f"{context} must be a nonempty string")
    return value


def _expect_int(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CatalogCompileError(f"{context} must be an integer")
    return value


def _expect_bool(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise CatalogCompileError(f"{context} must be a boolean")
    return value


def _expect_strings(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CatalogCompileError(f"{context} must be an array")
    strings = tuple(_expect_string(item, f"{context} item") for item in value)
    if len(set(strings)) != len(strings):
        raise CatalogCompileError(f"{context} contains duplicate values")
    return strings


def _expect_semver(value: object, context: str) -> str:
    version = _expect_string(value, context)
    if _SEMVER_RE.fullmatch(version) is None:
        raise CatalogCompileError(f"{context} must be a semantic version")
    return version


def _parse_definition(raw: object, index: int) -> DiagnosticDefinition:
    context = f"definition[{index}]"
    item = _expect_object(raw, _DEFINITION_FIELDS, context)
    code_text = _expect_string(item["code"], f"{context}.code")
    try:
        code = DiagnosticCode.parse(code_text)
    except DiagnosticCodeError as exc:
        raise CatalogCompileError(f"{context}.code: {exc}") from exc
    namespace = _expect_string(item["namespace"], f"{context}.namespace")
    try:
        condition_class = ConditionClass(_expect_int(item["condition_class"], f"{context}.condition_class"))
    except ValueError as exc:
        raise CatalogCompileError(f"{context}.condition_class is invalid") from exc
    if namespace != code.namespace:
        raise CatalogCompileError(f"{context} namespace contradicts code")
    if condition_class is not code.condition_class:
        raise CatalogCompileError(f"{context} condition class contradicts code")
    try:
        severity = DiagnosticSeverity(_expect_string(item["default_severity"], f"{context}.default_severity"))
        status = DefinitionStatus(_expect_string(item["status"], f"{context}.status"))
    except ValueError as exc:
        raise CatalogCompileError(f"{context} has invalid severity or status") from exc
    if status is not DefinitionStatus.ACTIVE:
        raise CatalogCompileError(f"{context} active definition must have active status")
    explanation_path = _validate_explanation_path(
        _expect_string(item["explanation_path"], f"{context}.explanation_path"),
        code_text,
    )
    return DiagnosticDefinition(
        code=code,
        title=_expect_string(item["title"], f"{context}.title"),
        summary=_expect_string(item["summary"], f"{context}.summary"),
        namespace=namespace,
        condition_class=condition_class,
        class_justification=_expect_string(item["class_justification"], f"{context}.class_justification"),
        default_severity=severity,
        default_blocking=_expect_bool(item["default_blocking"], f"{context}.default_blocking"),
        default_fatal=_expect_bool(item["default_fatal"], f"{context}.default_fatal"),
        default_retryable=_expect_bool(item["default_retryable"], f"{context}.default_retryable"),
        owner=_expect_string(item["owner"], f"{context}.owner"),
        introduction_version=_expect_semver(item["introduction_version"], f"{context}.introduction_version"),
        status=status,
        explanation_path=explanation_path,
        related_codes=_expect_strings(item["related_codes"], f"{context}.related_codes"),
        documentation_references=_expect_strings(
            item["documentation_references"],
            f"{context}.documentation_references",
        ),
        supported_phases=_expect_strings(item["supported_phases"], f"{context}.supported_phases"),
    )


def _parse_tombstone(raw: object, index: int) -> DiagnosticTombstone:
    context = f"tombstone[{index}]"
    item = _expect_object(raw, _TOMBSTONE_FIELDS, context)
    try:
        code = DiagnosticCode.parse(_expect_string(item["code"], f"{context}.code"))
    except DiagnosticCodeError as exc:
        raise CatalogCompileError(f"{context}.code: {exc}") from exc
    replacement = item["replacement_code"]
    if replacement is not None and not isinstance(replacement, str):
        raise CatalogCompileError(f"{context}.replacement_code must be a string or null")
    return DiagnosticTombstone(
        code=code,
        original_title=_expect_string(item["original_title"], f"{context}.original_title"),
        introduction_version=_expect_semver(item["introduction_version"], f"{context}.introduction_version"),
        retirement_version=_expect_semver(item["retirement_version"], f"{context}.retirement_version"),
        reason=_expect_string(item["reason"], f"{context}.reason"),
        replacement_code=replacement,
    )


def _validate_explanation_path(value: str, code: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts != ("explanations", f"{code}.md"):
        raise CatalogCompileError(f"invalid explanation path for {code}")
    return value


def _parse_allocation_ceilings(value: object) -> dict[tuple[str, int], int]:
    if not isinstance(value, dict):
        raise CatalogCompileError("allocation_ceilings must be an object")
    result: dict[tuple[str, int], int] = {}
    for namespace, raw_classes in value.items():
        registered = NAMESPACE_BY_TOKEN.get(namespace)
        if registered is None or registered.state is NamespaceState.RESERVED:
            raise CatalogCompileError(f"allocation ceiling uses unavailable namespace: {namespace}")
        if not isinstance(raw_classes, dict):
            raise CatalogCompileError(f"allocation ceiling for {namespace} must be an object")
        for class_digit, raw_ceiling in raw_classes.items():
            if class_digit not in tuple(str(value) for value in range(9)):
                raise CatalogCompileError(f"allocation ceiling uses invalid class: {namespace}{class_digit}")
            ceiling = _expect_int(raw_ceiling, f"allocation ceiling {namespace}{class_digit}")
            if not 1 <= ceiling <= 999:
                raise CatalogCompileError(f"allocation ceiling is out of range: {namespace}{class_digit}")
            result[(namespace, int(class_digit))] = ceiling
    return result


def compile_catalog(source_root: Path = CATALOG_ROOT) -> tuple[DiagnosticCatalog, bytes]:
    """Validate canonical source and return immutable models plus canonical JSON."""
    raw = _load_json(source_root / "source.json")
    schema_version = _expect_int(raw["diagnostic_schema_version"], "diagnostic_schema_version")
    catalog_version = _expect_int(raw["diagnostic_catalog_version"], "diagnostic_catalog_version")
    if schema_version != DIAGNOSTIC_SCHEMA_VERSION or catalog_version != DIAGNOSTIC_CATALOG_VERSION:
        raise CatalogCompileError("unsupported diagnostic schema or catalog version")
    raw_definitions = raw["definitions"]
    raw_tombstones = raw["tombstones"]
    if not isinstance(raw_definitions, list) or not isinstance(raw_tombstones, list):
        raise CatalogCompileError("definitions and tombstones must be arrays")
    definitions = tuple(_parse_definition(item, index) for index, item in enumerate(raw_definitions))
    tombstones = tuple(_parse_tombstone(item, index) for index, item in enumerate(raw_tombstones))
    _validate_catalog(definitions, tombstones, raw["allocation_ceilings"], source_root)
    catalog = DiagnosticCatalog(
        diagnostic_schema_version=schema_version,
        diagnostic_catalog_version=catalog_version,
        namespaces=NAMESPACES,
        definitions=tuple(sorted(definitions, key=lambda item: item.code)),
        tombstones=tuple(sorted(tombstones, key=lambda item: item.code)),
    )
    return catalog, catalog_bytes(catalog)


def _validate_catalog(
    definitions: tuple[DiagnosticDefinition, ...],
    tombstones: tuple[DiagnosticTombstone, ...],
    raw_ceilings: object,
    source_root: Path,
) -> None:
    active_codes = [str(item.code) for item in definitions]
    retired_codes = [str(item.code) for item in tombstones]
    _validate_code_uniqueness(active_codes, retired_codes)
    _validate_definition_references(definitions, set(active_codes) | set(retired_codes))
    _validate_documentation_references(definitions)
    _validate_tombstone_replacements(tombstones, set(active_codes))
    _validate_allocation_ceilings(definitions, tombstones, _parse_allocation_ceilings(raw_ceilings))
    _validate_explanations(definitions, source_root)
    _validate_bindings(definitions)


def _validate_code_uniqueness(active_codes: list[str], retired_codes: list[str]) -> None:
    if len(set(active_codes)) != len(active_codes):
        raise CatalogCompileError("duplicate active diagnostic code")
    if len(set(retired_codes)) != len(retired_codes):
        raise CatalogCompileError("duplicate diagnostic tombstone")
    if set(active_codes) & set(retired_codes):
        raise CatalogCompileError("active diagnostic reuses a tombstoned code")


def _validate_definition_references(
    definitions: tuple[DiagnosticDefinition, ...],
    known_codes: set[str],
) -> None:
    for item in definitions:
        if item.namespace != item.code.namespace or item.condition_class is not item.code.condition_class:
            raise CatalogCompileError(f"definition contradicts code metadata: {item.code}")
        if not item.supported_phases:
            raise CatalogCompileError(f"definition has no supported phase: {item.code}")
        if tuple(sorted(item.related_codes)) != item.related_codes:
            raise CatalogCompileError(f"related codes must be sorted: {item.code}")
        if str(item.code) in item.related_codes:
            raise CatalogCompileError(f"self-related diagnostic is unsupported: {item.code}")
        for related in item.related_codes:
            if related not in known_codes:
                raise CatalogCompileError(f"broken related-code reference: {item.code} -> {related}")


def _validate_tombstone_replacements(
    tombstones: tuple[DiagnosticTombstone, ...],
    active_codes: set[str],
) -> None:
    for tombstone in tombstones:
        if tombstone.replacement_code is not None and (
            tombstone.replacement_code == str(tombstone.code) or tombstone.replacement_code not in set(active_codes)
        ):
            raise CatalogCompileError(f"broken tombstone replacement: {tombstone.code}")


def _validate_documentation_references(definitions: tuple[DiagnosticDefinition, ...]) -> None:
    for definition in definitions:
        for reference in definition.documentation_references:
            path = PurePosixPath(reference)
            if path.is_absolute() or ".." in path.parts or not (REPOSITORY_ROOT / path).is_file():
                raise CatalogCompileError(f"broken documentation reference: {definition.code} -> {reference}")


def _validate_allocation_ceilings(
    definitions: tuple[DiagnosticDefinition, ...],
    tombstones: tuple[DiagnosticTombstone, ...],
    ceilings: Mapping[tuple[str, int], int],
) -> None:
    allocated: dict[tuple[str, int], list[int]] = {}
    for definition in definitions:
        key = (definition.code.namespace, int(definition.code.condition_class))
        allocated.setdefault(key, []).append(definition.code.ordinal)
    for tombstone in tombstones:
        key = (tombstone.code.namespace, int(tombstone.code.condition_class))
        allocated.setdefault(key, []).append(tombstone.code.ordinal)
    if set(ceilings) != set(allocated):
        raise CatalogCompileError("allocation ceilings do not match allocated namespace/classes")
    for key, ordinals in allocated.items():
        if max(ordinals) != ceilings[key]:
            raise CatalogCompileError(f"allocation ceiling does not match highest ordinal: {key[0]}{key[1]}")


def _validate_explanations(definitions: tuple[DiagnosticDefinition, ...], source_root: Path) -> None:
    expected = {Path(item.explanation_path) for item in definitions}
    explanation_root = source_root / "explanations"
    actual = (
        {
            path.relative_to(source_root)
            for path in explanation_root.iterdir()
            if path.is_file() and path.suffix == ".md"
        }
        if explanation_root.is_dir()
        else set()
    )
    missing = sorted(expected - actual)
    orphaned = sorted(actual - expected)
    if missing:
        raise CatalogCompileError(f"missing explanation: {missing[0].as_posix()}")
    if orphaned:
        raise CatalogCompileError(f"orphan explanation: {orphaned[0].as_posix()}")
    known_codes = {str(definition.code) for definition in definitions}
    for item in definitions:
        _validate_explanation(item, source_root, known_codes)


def _validate_explanation(
    item: DiagnosticDefinition,
    source_root: Path,
    known_codes: set[str],
) -> None:
    path = source_root / item.explanation_path
    text = _read_bounded(path, MAX_EXPLANATION_BYTES).decode("utf-8")
    if not text.startswith(f"# {item.code} — "):
        raise CatalogCompileError(f"explanation heading/code mismatch: {item.code}")
    invalid_headings = [heading for heading in REQUIRED_EXPLANATION_HEADINGS if text.count(heading) != 1]
    if invalid_headings:
        raise CatalogCompileError(f"explanation {item.code} must contain exactly one {invalid_headings[0]}")
    if re.search(r"SENTINEL|BEGIN (?:RSA )?PRIVATE KEY|AKIA[0-9A-Z]{16}", text, re.IGNORECASE):
        raise CatalogCompileError(f"explanation contains a secret sentinel: {item.code}")
    _validate_markdown_links(text, path)
    mentioned_codes = set(re.findall(r"\bSW[A-Z]+[0-9]{4}\b", text))
    if not mentioned_codes <= known_codes:
        raise CatalogCompileError(f"explanation has unknown related code: {item.code}")


def _validate_markdown_links(text: str, explanation_path: Path) -> None:
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        if target.startswith("#"):
            continue
        if "://" in target or target.startswith("mailto:"):
            raise CatalogCompileError(f"explanation link must be local: {explanation_path.name}")
        selected = explanation_path.parent / target.split("#", 1)[0]
        if not selected.is_file():
            raise CatalogCompileError(f"explanation has broken local link: {explanation_path.name}")


def _validate_bindings(definitions: tuple[DiagnosticDefinition, ...]) -> None:
    by_code = {str(item.code): item for item in definitions}
    _validate_configuration_bindings(by_code)
    _validate_segment_bindings(by_code)
    _validate_runtime_bindings(by_code)


def _validate_configuration_bindings(by_code: dict[str, DiagnosticDefinition]) -> None:
    binding_codes = {binding.code for binding in RULE_BINDINGS} | set(RELOAD_CODES.values())
    swcfg_codes = {code for code in by_code if code.startswith("SWCFG")}
    if binding_codes != swcfg_codes:
        raise CatalogCompileError("active SWCFG codes and P1-11 rule bindings differ")
    if len({binding.rule_id for binding in RULE_BINDINGS}) != len(RULE_BINDINGS):
        raise CatalogCompileError("duplicate P1-11 rule binding")
    for binding in RULE_BINDINGS:
        _validate_configuration_binding(binding, by_code.get(binding.code))


def _validate_configuration_binding(
    binding: RuleCodeBinding,
    definition: DiagnosticDefinition | None,
) -> None:
    if definition is None:
        raise CatalogCompileError(f"binding has no active catalog definition: {binding.rule_id}")
    if definition.namespace != "SWCFG" or binding.phase not in definition.supported_phases:
        raise CatalogCompileError(f"binding contradicts catalog definition: {binding.rule_id}")


def _validate_runtime_bindings(by_code: dict[str, DiagnosticDefinition]) -> None:
    runtime_codes = set(RUNTIME_CODES.values())
    nwws_codes = set(NWWS_CODES.values())
    if len(runtime_codes) != len(RUNTIME_CODES) or not runtime_codes.issubset(by_code):
        raise CatalogCompileError("runtime code bindings are incomplete or duplicated")
    if len(nwws_codes) != len(NWWS_CODES) or not nwws_codes.issubset(by_code):
        raise CatalogCompileError("NWWS source diagnostic bindings are incomplete or duplicated")


def _validate_segment_bindings(by_code: dict[str, DiagnosticDefinition]) -> None:
    codes = {binding.code for binding in SEGMENT_BINDINGS}
    segment_codes = {code for code in by_code if code.startswith("SWSEG")}
    if codes != segment_codes:
        raise CatalogCompileError("active SWSEG codes and segment registry bindings differ")
    if len({binding.rule_id for binding in SEGMENT_BINDINGS}) != len(SEGMENT_BINDINGS):
        raise CatalogCompileError("duplicate segment registry binding")
    for binding in SEGMENT_BINDINGS:
        _validate_segment_binding(binding, by_code.get(binding.code))


def _validate_segment_binding(
    binding: RuleCodeBinding,
    definition: DiagnosticDefinition | None,
) -> None:
    if definition is None:
        raise CatalogCompileError(f"binding has no active catalog definition: {binding.rule_id}")
    if definition.namespace != "SWSEG" or binding.phase not in definition.supported_phases:
        raise CatalogCompileError(f"binding contradicts segment catalog definition: {binding.rule_id}")


def catalog_dict(catalog: DiagnosticCatalog) -> dict[str, object]:
    return {
        "diagnostic_schema_version": catalog.diagnostic_schema_version,
        "diagnostic_catalog_version": catalog.diagnostic_catalog_version,
        "namespaces": [
            {
                "token": item.token,
                "state": item.state.value,
                "scope": item.scope,
                "owner": item.owner,
                "remediation_domain": item.remediation_domain,
            }
            for item in catalog.namespaces
        ],
        "definitions": [
            {
                "code": str(item.code),
                "title": item.title,
                "summary": item.summary,
                "namespace": item.namespace,
                "condition_class": int(item.condition_class),
                "class_justification": item.class_justification,
                "default_severity": item.default_severity.value,
                "default_blocking": item.default_blocking,
                "default_fatal": item.default_fatal,
                "default_retryable": item.default_retryable,
                "owner": item.owner,
                "introduction_version": item.introduction_version,
                "status": item.status.value,
                "explanation_path": item.explanation_path,
                "related_codes": list(item.related_codes),
                "documentation_references": list(item.documentation_references),
                "supported_phases": list(item.supported_phases),
            }
            for item in catalog.definitions
        ],
        "tombstones": [
            {
                "code": str(item.code),
                "original_title": item.original_title,
                "introduction_version": item.introduction_version,
                "retirement_version": item.retirement_version,
                "reason": item.reason,
                "replacement_code": item.replacement_code,
            }
            for item in catalog.tombstones
        ],
    }


def catalog_bytes(catalog: DiagnosticCatalog) -> bytes:
    return (
        json.dumps(
            catalog_dict(catalog),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise CatalogCompileError(f"could not write compiled catalog: {path.name}") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def build(output: Path = GENERATED_PATH, *, source_root: Path = CATALOG_ROOT) -> bytes:
    _, content = compile_catalog(source_root)
    write_atomic(output, content)
    return content


def check(generated: Path = GENERATED_PATH, *, source_root: Path = CATALOG_ROOT) -> None:
    _, expected = compile_catalog(source_root)
    actual = _read_bounded(generated, MAX_SOURCE_BYTES)
    if actual != expected:
        raise CatalogCompileError("generated diagnostic catalog has drifted from canonical source")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m seasonalweather.diagnostics.compiler")
    parser.add_argument("command", choices=("check", "build"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            check(args.output or GENERATED_PATH)
        else:
            build(args.output or GENERATED_PATH)
    except CatalogCompileError as exc:
        parser.exit(1, f"diagnostics {args.command} failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
