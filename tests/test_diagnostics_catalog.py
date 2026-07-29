from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from seasonalweather.diagnostics.bindings import RULE_BINDINGS, binding_for_rule
from seasonalweather.diagnostics.codes import (
    ConditionClass,
    DiagnosticCode,
    DiagnosticCodeError,
    format_code,
)
from seasonalweather.diagnostics.compiler import (
    CATALOG_ROOT,
    CatalogCompileError,
    build,
    compile_catalog,
)
from seasonalweather.diagnostics.exporter import CatalogExportError, export_catalog
from seasonalweather.diagnostics.loader import (
    CatalogLoadError,
    _from_compiled,
    load_catalog,
    load_explanation,
    packaged_catalog_bytes,
)
from seasonalweather.diagnostics.models import (
    DefinitionStatus,
    DiagnosticCatalog,
    DiagnosticTombstone,
)
from seasonalweather.diagnostics.namespaces import NAMESPACE_BY_TOKEN, NAMESPACES, NamespaceState
from seasonalweather.diagnostics.registry import CatalogLookupError, DiagnosticCatalogService
from seasonalweather.diagnostics.representations import (
    detail_representation,
    explanation_representation,
    list_representation,
    namespace_list_representation,
    tombstone_representation,
    unknown_representation,
)

ROOT = Path(__file__).resolve().parents[1]
CODE_PATTERN = re.compile(r'"((?:source|yaml|schema|compiler)\.[a-z_.]+)"')


def _copy_catalog(tmp_path: Path) -> Path:
    target = tmp_path / "catalog"
    shutil.copytree(CATALOG_ROOT, target)
    return target


def _source(root: Path) -> dict[str, object]:
    return json.loads((root / "source.json").read_text(encoding="utf-8"))


def _write_source(root: Path, value: dict[str, object]) -> None:
    (root / "source.json").write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def test_namespace_registry_is_exact_typed_and_reserved_states_are_visible() -> None:
    assert {item.token for item in NAMESPACES if item.state is NamespaceState.ACTIVE} == {
        "SWCFG",
        "SWRUN",
        "SWCAP",
        "SWNWWS",
        "SWERN",
        "SWTTS",
        "SWSEG",
        "SWLQS",
        "SWJOB",
        "SWWP",
        "SWDB",
        "SWOBS",
        "SWBUILD",
    }
    assert NAMESPACE_BY_TOKEN["SWERN"].state is NamespaceState.ACTIVE
    assert NAMESPACE_BY_TOKEN["SWCACHE"].state is NamespaceState.RESERVED
    assert NAMESPACE_BY_TOKEN["SWREDIS"].state is NamespaceState.RESERVED
    assert "FFmpeg" in NAMESPACE_BY_TOKEN["SWERN"].scope
    assert all(item.owner and item.remediation_domain for item in NAMESPACES)


@pytest.mark.parametrize("namespace", [item.token for item in NAMESPACES if item.state is NamespaceState.ACTIVE])
def test_every_active_namespace_parses_and_formats(namespace: str) -> None:
    code = format_code(namespace, ConditionClass.GENERAL, 1)
    parsed = DiagnosticCode.parse(code)

    assert str(parsed) == code
    assert parsed.namespace == namespace
    assert parsed.condition_class is ConditionClass.GENERAL
    assert parsed.ordinal == 1


@pytest.mark.parametrize("condition_class", list(range(9)))
@pytest.mark.parametrize("ordinal", [1, 999])
def test_every_assignable_class_and_ordinal_boundary_parses(condition_class: int, ordinal: int) -> None:
    code = format_code("SWNWWS", condition_class, ordinal)
    parsed = DiagnosticCode.parse(code)
    assert int(parsed.condition_class) == condition_class
    assert parsed.ordinal == ordinal


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("swcfg1001", "unknown_namespace"),
        ("SW-CFG1001", "unknown_namespace"),
        ("CFG1001", "unknown_namespace"),
        ("SWCFG1000", "class_boundary"),
        ("SWCFG2000", "class_boundary"),
        ("SWCFG9001", "reserved_class"),
        ("SWCFG10A1", "malformed"),
        ("SWCFG10001", "unknown_namespace"),
        ("SWCACHE1001", "reserved_namespace"),
        ("SWREDIS1001", "reserved_namespace"),
    ],
)
def test_invalid_codes_fail_with_bounded_typed_errors(value: str, kind: str) -> None:
    with pytest.raises(DiagnosticCodeError) as exc_info:
        DiagnosticCode.parse(value)
    assert exc_info.value.kind == kind


def test_canonical_catalog_is_immutable_complete_and_deterministic() -> None:
    first, first_bytes = compile_catalog()
    second, second_bytes = compile_catalog()

    assert first == second == load_catalog()
    assert first_bytes == second_bytes == packaged_catalog_bytes()
    assert len(first.definitions) == len(RULE_BINDINGS) == 28
    assert {definition.introduction_version for definition in first.definitions} == {"0.18.0"}
    assert not first.tombstones
    assert first_bytes.endswith(b"\n")
    assert b"/home/" not in first_bytes
    assert b"timestamp" not in first_bytes.lower()
    with pytest.raises(FrozenInstanceError):
        first.definitions[0].title = "changed"  # type: ignore[misc]


def test_source_order_does_not_change_compiled_bytes(tmp_path: Path) -> None:
    copied = _copy_catalog(tmp_path)
    raw = _source(copied)
    definitions = raw["definitions"]
    assert isinstance(definitions, list)
    definitions.reverse()
    _write_source(copied, raw)

    _, reordered = compile_catalog(copied)
    assert reordered == packaged_catalog_bytes()


def test_catalog_source_rejects_duplicate_and_unknown_metadata_keys(tmp_path: Path) -> None:
    copied = _copy_catalog(tmp_path)
    source_path = copied / "source.json"
    text = source_path.read_text(encoding="utf-8")
    source_path.write_text(
        text.replace(
            '"diagnostic_schema_version": 1,', '"diagnostic_schema_version": 1,\n  "diagnostic_schema_version": 1,'
        ),
        encoding="utf-8",
    )
    with pytest.raises(CatalogCompileError, match="duplicate metadata key"):
        compile_catalog(copied)

    copied = _copy_catalog(tmp_path / "unknown")
    raw = _source(copied)
    definitions = raw["definitions"]
    assert isinstance(definitions, list) and isinstance(definitions[0], dict)
    definitions[0]["unexpected"] = True
    _write_source(copied, raw)
    with pytest.raises(CatalogCompileError, match="unknown field"):
        compile_catalog(copied)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw["definitions"].append(raw["definitions"][0].copy()), "duplicate active"),
        (
            lambda raw: raw["definitions"][0].update({"namespace": "SWRUN"}),
            "namespace contradicts",
        ),
        (
            lambda raw: raw["definitions"][0].update({"condition_class": 2}),
            "condition class contradicts",
        ),
        (
            lambda raw: raw["definitions"][0].update({"default_severity": "critical"}),
            "invalid severity",
        ),
        (
            lambda raw: raw["definitions"][0].update({"owner": ""}),
            "nonempty string",
        ),
        (
            lambda raw: raw["definitions"][0].update({"introduction_version": "next"}),
            "semantic version",
        ),
        (
            lambda raw: raw["definitions"][0].update({"explanation_path": "../outside.md"}),
            "invalid explanation path",
        ),
        (
            lambda raw: raw["definitions"][0].update({"related_codes": ["SWCFG1999"]}),
            "broken related-code",
        ),
        (
            lambda raw: raw["definitions"][0].update({"related_codes": ["SWCFG1002", "SWCFG1002"]}),
            "duplicate values",
        ),
    ],
)
def test_catalog_metadata_contradictions_fail_closed(tmp_path: Path, mutation, message: str) -> None:
    copied = _copy_catalog(tmp_path)
    raw = _source(copied)
    mutation(raw)
    _write_source(copied, raw)

    with pytest.raises(CatalogCompileError, match=message):
        compile_catalog(copied)


def test_invalid_condition_class_is_a_bounded_compiler_error(tmp_path: Path) -> None:
    copied = _copy_catalog(tmp_path)
    raw = _source(copied)
    definitions = raw["definitions"]
    assert isinstance(definitions, list) and isinstance(definitions[0], dict)
    definitions[0]["condition_class"] = 42
    _write_source(copied, raw)

    with pytest.raises(CatalogCompileError, match="condition_class is invalid"):
        compile_catalog(copied)


def test_broken_documentation_reference_fails_closed(tmp_path: Path) -> None:
    copied = _copy_catalog(tmp_path)
    raw = _source(copied)
    definitions = raw["definitions"]
    assert isinstance(definitions, list) and isinstance(definitions[0], dict)
    definitions[0]["documentation_references"] = ["docs/not-present.md"]
    _write_source(copied, raw)

    with pytest.raises(CatalogCompileError, match="broken documentation reference"):
        compile_catalog(copied)


def test_reserved_x000_and_9xxx_assignments_fail_closed(tmp_path: Path) -> None:
    for code in ("SWCACHE1001", "SWCFG1000", "SWCFG9001"):
        copied = _copy_catalog(tmp_path / code)
        raw = _source(copied)
        definitions = raw["definitions"]
        assert isinstance(definitions, list) and isinstance(definitions[0], dict)
        definitions[0]["code"] = code
        definitions[0]["namespace"] = code[:-4]
        definitions[0]["condition_class"] = int(code[-4])
        definitions[0]["explanation_path"] = f"explanations/{code}.md"
        _write_source(copied, raw)
        with pytest.raises(CatalogCompileError):
            compile_catalog(copied)


def test_tombstone_duplication_reuse_and_replacement_fail_closed(tmp_path: Path) -> None:
    copied = _copy_catalog(tmp_path)
    raw = _source(copied)
    tombstone = {
        "code": "SWCFG1001",
        "original_title": "Configuration source is not UTF-8",
        "introduction_version": "0.17.0",
        "retirement_version": "0.18.0",
        "reason": "Synthetic validation fixture.",
        "replacement_code": "SWCFG1002",
    }
    raw["tombstones"] = [tombstone]
    _write_source(copied, raw)
    with pytest.raises(CatalogCompileError, match="reuses a tombstoned"):
        compile_catalog(copied)

    raw["definitions"] = [item for item in raw["definitions"] if item["code"] != "SWCFG1001"]
    raw["tombstones"] = [tombstone, tombstone.copy()]
    _write_source(copied, raw)
    with pytest.raises(CatalogCompileError, match="duplicate diagnostic tombstone"):
        compile_catalog(copied)

    raw["tombstones"] = [{**tombstone, "replacement_code": "SWCFG1999"}]
    _write_source(copied, raw)
    with pytest.raises(CatalogCompileError, match="broken tombstone replacement"):
        compile_catalog(copied)


def test_allocation_ceiling_enforces_monotonic_review_state(tmp_path: Path) -> None:
    copied = _copy_catalog(tmp_path)
    raw = _source(copied)
    raw["allocation_ceilings"]["SWCFG"]["1"] = 19
    _write_source(copied, raw)
    with pytest.raises(CatalogCompileError, match="highest ordinal"):
        compile_catalog(copied)


def test_explanations_are_complete_sanitized_and_one_to_one(tmp_path: Path) -> None:
    catalog = load_catalog()
    paths = {item.explanation_path for item in catalog.definitions}
    assert len(paths) == len(catalog.definitions)
    for item in catalog.definitions:
        markdown = load_explanation(item.explanation_path)
        assert markdown.startswith(f"# {item.code} — ")
        assert "SENTINEL" not in markdown
        assert "BEGIN PRIVATE KEY" not in markdown

    copied = _copy_catalog(tmp_path)
    (copied / next(iter(paths))).unlink()
    with pytest.raises(CatalogCompileError, match="missing explanation"):
        compile_catalog(copied)

    copied = _copy_catalog(tmp_path / "orphan")
    (copied / "explanations/ORPHAN.md").write_text("# orphan\n", encoding="utf-8")
    with pytest.raises(CatalogCompileError, match="orphan explanation"):
        compile_catalog(copied)

    copied = _copy_catalog(tmp_path / "link")
    explanation = copied / "explanations/SWCFG1001.md"
    explanation.write_text(
        explanation.read_text(encoding="utf-8") + "\n[missing](missing.md)\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogCompileError, match="broken local link"):
        compile_catalog(copied)

    copied = _copy_catalog(tmp_path / "sentinel")
    explanation = copied / "explanations/SWCFG1001.md"
    explanation.write_text(
        explanation.read_text(encoding="utf-8") + "\nSENTINEL-SECRET\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogCompileError, match="secret sentinel"):
        compile_catalog(copied)


def test_invalid_compile_does_not_promote_partial_output(tmp_path: Path) -> None:
    copied = _copy_catalog(tmp_path)
    output = tmp_path / "generated.json"
    output.write_bytes(b"previous\n")
    (copied / "source.json").write_text("{invalid", encoding="utf-8")

    with pytest.raises(CatalogCompileError):
        build(output, source_root=copied)
    assert output.read_bytes() == b"previous\n"


def test_loader_works_outside_repository_cwd_and_resources_are_complete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    catalog = load_catalog()
    assert catalog.definitions
    assert all(load_explanation(item.explanation_path) for item in catalog.definitions)


def test_package_data_metadata_covers_compiled_source_and_explanations() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = metadata["tool"]["setuptools"]["package-data"]["seasonalweather.diagnostics"]

    assert "catalog/catalog.json" in declared
    assert "catalog/source.json" in declared
    assert "catalog/explanations/*.md" in declared
    assert len(tuple((CATALOG_ROOT / "explanations").glob("*.md"))) == 28


def test_corrupt_compiled_catalog_and_invalid_resource_paths_fail_bounded() -> None:
    raw = json.loads(packaged_catalog_bytes())
    raw["definitions"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="definition fields are invalid"):
        _from_compiled(raw)
    with pytest.raises(CatalogLoadError, match="missing or invalid"):
        load_explanation("../outside.md")


def test_export_is_clean_deterministic_and_matches_package(tmp_path: Path) -> None:
    destination = tmp_path / "usr/share/seasonalweather/diagnostics"
    destination.mkdir(parents=True)
    (destination / "stale.txt").write_text("stale", encoding="utf-8")

    assert export_catalog(destination) == destination
    assert not (destination / "stale.txt").exists()
    assert (destination / "catalog.json").read_bytes() == packaged_catalog_bytes()
    assert {path.name for path in (destination / "explanations").iterdir()} == {
        Path(item.explanation_path).name for item in load_catalog().definitions
    }

    symlink = tmp_path / "linked"
    symlink.symlink_to(destination, target_is_directory=True)
    with pytest.raises(CatalogExportError, match="symlink"):
        export_catalog(symlink)
    with pytest.raises(CatalogExportError, match="traversal"):
        export_catalog(tmp_path / "safe/../unsafe")


def test_every_p1_11_rule_has_one_active_explainable_mapping() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "seasonalweather/configuration").glob("*.py")
    )
    emitted = set(CODE_PATTERN.findall(source))
    mapped = {binding.rule_id for binding in RULE_BINDINGS}
    catalog = load_catalog()

    assert emitted == mapped
    assert len({binding.code for binding in RULE_BINDINGS}) == len(RULE_BINDINGS)
    for binding in RULE_BINDINGS:
        definition = catalog.definition(binding.code)
        assert definition is not None
        assert definition.status is DefinitionStatus.ACTIVE
        assert definition.namespace == "SWCFG"
        assert binding.phase in definition.supported_phases
        assert load_explanation(definition.explanation_path)
        assert binding_for_rule(binding.rule_id) is binding
    assert {str(item.code) for item in catalog.definitions} == {item.code for item in RULE_BINDINGS}


def test_public_representations_are_pure_deterministic_and_api_ready() -> None:
    catalog = load_catalog()
    definition = catalog.definitions[0]
    markdown = load_explanation(definition.explanation_path)
    values = (
        namespace_list_representation(catalog),
        list_representation(catalog),
        detail_representation(catalog, definition),
        explanation_representation(catalog, definition, markdown),
        unknown_representation(catalog, "SWCFG1999"),
    )
    for value in values:
        encoded = json.dumps(value, sort_keys=True)
        assert "/home/" not in encoded
        assert "occurrence" not in encoded
        assert "diagnostic_schema_version" in value
        assert "diagnostic_catalog_version" in value


def test_tombstone_lookup_and_representation_are_distinct() -> None:
    catalog = load_catalog()
    retired = DiagnosticTombstone(
        code=DiagnosticCode.parse("SWCFG1999"),
        original_title="Retired synthetic condition",
        introduction_version="0.17.0",
        retirement_version="0.18.0",
        reason="Test-only tombstone.",
        replacement_code=None,
    )
    with_tombstone = DiagnosticCatalog(
        diagnostic_schema_version=catalog.diagnostic_schema_version,
        diagnostic_catalog_version=catalog.diagnostic_catalog_version,
        namespaces=catalog.namespaces,
        definitions=catalog.definitions,
        tombstones=(retired,),
    )
    service = DiagnosticCatalogService(with_tombstone)

    with pytest.raises(CatalogLookupError) as exc_info:
        service.lookup("SWCFG1999")
    assert exc_info.value.kind == "retired"
    assert tombstone_representation(with_tombstone, retired)["diagnostic"]["status"] == "retired"
