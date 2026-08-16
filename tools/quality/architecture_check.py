from __future__ import annotations

import argparse
import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.quality.governance import ROOT, load_toml


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def _matches_prefix(value: str, prefixes: Iterable[str]) -> bool:
    return any(value == prefix or value.startswith(f"{prefix}.") for prefix in prefixes)


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_import(node: ast.ImportFrom, module: str) -> str:
    imported = node.module or ""
    if node.level == 0:
        return imported
    package = module.split(".")
    if package and package[-1] != "__init__":
        package.pop()
    trim = max(0, node.level - 1)
    if trim:
        package = package[:-trim]
    return ".".join([*package, imported] if imported else package)


def _imports(tree: ast.AST, module: str) -> Iterable[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            yield _resolve_import(node, module), node.lineno


def _qualified_call(node: ast.Call) -> str:
    parts: list[str] = []
    current: ast.expr = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _path_variables(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if isinstance(value, ast.Call) and _qualified_call(value) in {"Path", "pathlib.Path"}:
                names.update(target.id for target in targets if isinstance(target, ast.Name))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                annotation = argument.annotation
                if isinstance(annotation, ast.Name) and annotation.id == "Path":
                    names.add(argument.arg)
    return names


def _open_mutates(node: ast.Call) -> bool:
    if _qualified_call(node) != "open":
        return False
    mode: object = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            mode = keyword.value.value
    return isinstance(mode, str) and any(flag in mode for flag in "wax+")


def _filesystem_mutation(node: ast.Call, path_variables: set[str]) -> str | None:
    call = _qualified_call(node)
    if call in {
        "os.makedirs",
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.replace",
        "os.unlink",
        "Path.mkdir",
        "Path.rename",
        "Path.replace",
        "Path.touch",
        "Path.unlink",
        "Path.write_bytes",
        "Path.write_text",
        "pathlib.Path.mkdir",
        "pathlib.Path.rename",
        "pathlib.Path.replace",
        "pathlib.Path.touch",
        "pathlib.Path.unlink",
        "pathlib.Path.write_bytes",
        "pathlib.Path.write_text",
    }:
        return call
    if _open_mutates(node):
        return call
    if not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr not in {"mkdir", "rename", "replace", "touch", "unlink", "write_bytes", "write_text"}:
        return None
    owner = node.func.value
    if isinstance(owner, ast.Name) and owner.id in path_variables:
        return f"{owner.id}.{node.func.attr}"
    if isinstance(owner, ast.Call) and _qualified_call(owner) in {"Path", "pathlib.Path"}:
        return f"{_qualified_call(owner)}.{node.func.attr}"
    return None


def _under(path: str, roots: Iterable[str]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


def _exception_applies(finding: Finding, exceptions: list[dict[str, Any]]) -> bool:
    return any(
        item.get("rule") == finding.rule
        and (finding.path == item.get("scope") or finding.path.startswith(f"{item.get('scope', '')}/"))
        for item in exceptions
    )


def scan(root: Path, config: dict[str, Any], exceptions: list[dict[str, Any]] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    exceptions = exceptions or []
    worker_roots = config["worker_roots"]
    controller_roots = config["controller_roots"]

    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if any(part in {".git", ".venv", ".venv-ci", "__pycache__"} for part in path.parts):
            continue
        if relative.startswith("tests/architecture/fixtures/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except SyntaxError as exc:
            findings.append(Finding(relative, exc.lineno or 1, "SWARCH000", "Python source must parse."))
            continue
        module = _module_name(path, root)
        imports = list(_imports(tree, module))
        path_variables = _path_variables(tree)
        is_worker = _matches_prefix(module, worker_roots)
        is_controller = _matches_prefix(module, controller_roots) and not is_worker

        if is_controller:
            for imported, line in imports:
                if _matches_prefix(imported, config["worker_only_imports"]):
                    findings.append(
                        Finding(relative, line, "SWARCH001", f"controller imports worker-only module {imported}")
                    )
        if is_worker:
            for imported, line in imports:
                if _matches_prefix(imported, config["controller_authority_imports"]):
                    findings.append(
                        Finding(relative, line, "SWARCH002", f"worker imports controller authority {imported}")
                    )

        if _under(relative, config["api_roots"]):
            for imported, line in imports:
                if _matches_prefix(imported, config["api_forbidden_imports"]):
                    findings.append(Finding(relative, line, "SWARCH003", f"API imports mutation authority {imported}"))
                if _matches_prefix(imported, config.get("api_diagnostics_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH023",
                            f"API imports diagnostic catalog file authority {imported}",
                        )
                    )
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    mutation = _filesystem_mutation(node, path_variables)
                    if mutation:
                        findings.append(
                            Finding(
                                relative, node.lineno, "SWARCH003", f"API performs filesystem mutation via {mutation}"
                            )
                        )

        if _under(relative, config["domain_roots"]):
            for imported, line in imports:
                if _matches_prefix(imported, config["domain_forbidden_imports"]):
                    findings.append(
                        Finding(relative, line, "SWARCH004", f"domain/validation imports deployment concern {imported}")
                    )

        if _under(relative, config.get("validation_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("validation_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH027",
                            f"validation imports runtime or mutation authority {imported}",
                        )
                    )
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    mutation = _filesystem_mutation(node, path_variables)
                    if mutation:
                        findings.append(
                            Finding(
                                relative,
                                node.lineno,
                                "SWARCH028",
                                f"validation performs dependency mutation via {mutation}",
                            )
                        )

        if _under(relative, config.get("reload_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("reload_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH029",
                            f"reload package imports future or deployment authority {imported}",
                        )
                    )

        if _under(relative, config.get("reload_validation_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("reload_validation_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative, line, "SWARCH030", f"reload validation imports active-state authority {imported}"
                        )
                    )

        if _under(relative, config.get("api_roots", [])) and relative not in config.get(
            "api_reload_composition_allowed", []
        ):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("api_reload_authority_imports", [])):
                    findings.append(
                        Finding(relative, line, "SWARCH031", f"API route imports reload mutation authority {imported}")
                    )

        if relative == "seasonalweather/control.py":
            for imported, line in imports:
                if _matches_prefix(imported, config.get("control_reload_authority_imports", [])):
                    findings.append(
                        Finding(relative, line, "SWARCH032", f"control module retains reload authority {imported}")
                    )

        if _under(relative, config.get("cli_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("cli_reload_authority_imports", [])):
                    findings.append(
                        Finding(relative, line, "SWARCH033", f"CLI imports reload mutation authority {imported}")
                    )

        if _under(relative, config.get("contract_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("contract_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH008",
                            f"command/job contract imports runtime authority {imported}",
                        )
                    )

        if _under(relative, config.get("tts_contract_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("tts_contract_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative, line, "SWARCH034", f"TTS contract imports API or mutation authority {imported}"
                        )
                    )

        if _under(relative, config.get("tts_local_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("tts_local_forbidden_imports", [])):
                    findings.append(
                        Finding(relative, line, "SWARCH035", f"local TTS owner imports forbidden authority {imported}")
                    )

        if _under(relative, config.get("tts_policy_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("tts_policy_forbidden_imports", [])):
                    findings.append(
                        Finding(relative, line, "SWARCH036", f"TTS policy crosses the engine boundary {imported}")
                    )

        if _under(relative, config.get("tts_provider_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("tts_provider_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH037",
                            f"TTS provider adapter imports controller or publication authority {imported}",
                        )
                    )

        if _under(relative, config.get("tts_transport_roots", [])) and not _under(
            relative, config.get("tts_transport_allowed_roots", [])
        ):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("tts_transport_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH038",
                            f"provider transport dependency escapes the adapter package {imported}",
                        )
                    )

        if _under(relative, config.get("tts_caller_roots", [])):
            source = path.read_text(encoding="utf-8")
            for term in config.get("tts_provider_wire_terms", []):
                if term in source:
                    line = next(
                        (index for index, value in enumerate(source.splitlines(), start=1) if term in value),
                        1,
                    )
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH039",
                            f"provider wire detail appears in a TTS caller {term!r}",
                        )
                    )

        if _under(relative, config.get("configuration_core_roots", [])) and not _under(
            relative,
            config.get("configuration_adapter_roots", []),
        ):
            for imported, line in imports:
                if _matches_prefix(
                    imported,
                    config.get("configuration_forbidden_imports", []),
                ):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH018",
                            f"configuration compiler imports runtime authority {imported}",
                        )
                    )

        if _under(relative, config.get("diagnostics_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("diagnostics_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH020",
                            f"diagnostic catalog imports runtime authority {imported}",
                        )
                    )
            if relative not in config.get("diagnostics_file_authorities", []) and relative != config.get(
                "diagnostics_resource_loader"
            ):
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        mutation = _filesystem_mutation(node, path_variables)
                        if mutation:
                            findings.append(
                                Finding(
                                    relative,
                                    node.lineno,
                                    "SWARCH024",
                                    f"diagnostic catalog has mutable file path via {mutation}",
                                )
                            )

        if _under(relative, config.get("api_roots", [])) and relative not in config.get(
            "api_runtime_diagnostics_allowed", []
        ):
            for imported, line in imports:
                if _matches_prefix(imported, ("seasonalweather.runtime_diagnostics",)):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH025",
                            f"API route imports mutable runtime diagnostic authority {imported}",
                        )
                    )

        if relative == config.get("runtime_fatal_renderer"):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("runtime_fatal_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH026",
                            f"fatal emergency path imports unsafe runtime dependency {imported}",
                        )
                    )

        if _under(relative, config.get("nwws_source_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("nwws_forbidden_imports", [])) and not _under(
                    relative,
                    config.get("nwws_adapter_roots", []),
                ):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH040",
                            f"NWWS contract/helper imports transport or job authority {imported}",
                        )
                    )
                elif _matches_prefix(imported, config.get("nwws_forbidden_imports", [])) and _under(
                    relative,
                    config.get("nwws_adapter_roots", []),
                ) and imported != "slixmpp":
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH041",
                            f"NWWS adapter imports job or deployment authority {imported}",
                        )
                    )

        if relative in config.get("nwws_consumer_roots", []):
            for imported, line in imports:
                if _matches_prefix(imported, ("slixmpp",)):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH042",
                            "NWWS consumer imports slixmpp instead of the normalized source boundary",
                        )
                    )

        if relative.startswith("seasonalweather/") and not _under(
            relative,
            config.get("nwws_slixmpp_allowed_roots", []),
        ):
            for imported, line in imports:
                if _matches_prefix(imported, ("slixmpp",)):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH043",
                            "production code imports slixmpp outside the NWWS adapter seam",
                        )
                    )

        if relative.startswith("seasonalweather/") and relative != config.get("diagnostics_resource_loader"):
            for imported, line in imports:
                if _matches_prefix(imported, ("importlib.resources",)):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH022",
                            f"package-resource catalog loading bypasses owned loader via {imported}",
                        )
                    )

        if relative.startswith("seasonalweather/") and relative != config.get("configuration_yaml_parser"):
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                call = _qualified_call(node)
                if call in {
                    "yaml.load",
                    "yaml.safe_load",
                    "yaml.full_load",
                    "yaml.load_all",
                    "yaml.safe_load_all",
                    "yaml.full_load_all",
                }:
                    findings.append(
                        Finding(
                            relative,
                            node.lineno,
                            "SWARCH019",
                            f"configuration YAML parsing bypasses owned parser via {call}",
                        )
                    )

        if relative.startswith("seasonalweather/") and not _under(
            relative,
            config.get("sqlite_owner_roots", []),
        ):
            for imported, line in imports:
                if imported == "sqlite3":
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH009",
                            "SQLite access must remain in an owned database/repository module",
                        )
                    )

        if _under(relative, config.get("job_store_roots", [])):
            for imported, line in imports:
                if _matches_prefix(
                    imported,
                    config.get("job_store_forbidden_imports", []),
                ):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH010",
                            f"job repository imports execution/publication authority {imported}",
                        )
                    )

        if _under(relative, config.get("swwp_roots", [])):
            for imported, line in imports:
                if _matches_prefix(
                    imported,
                    config.get("swwp_forbidden_imports", []),
                ):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH011",
                            f"SWWP imports transport/execution/publication authority {imported}",
                        )
                    )
                if not _under(
                    relative,
                    config.get("swwp_adapter_roots", []),
                ) and _matches_prefix(
                    imported,
                    config.get("swwp_non_adapter_forbidden_imports", []),
                ):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH012",
                            f"SWWP state/schema module imports durable queue authority {imported}",
                        )
                    )

        if _under(relative, config.get("capability_roots", [])):
            for imported, line in imports:
                if _matches_prefix(
                    imported,
                    config.get("capability_forbidden_imports", []),
                ):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH013",
                            f"capability package imports execution/publication authority {imported}",
                        )
                    )
                if not _under(
                    relative,
                    config.get("capability_adapter_roots", []),
                ) and _matches_prefix(
                    imported,
                    config.get("capability_non_adapter_forbidden_imports", []),
                ):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH014",
                            f"capability model imports scheduler/protocol authority {imported}",
                        )
                    )

        if _under(relative, config.get("artifact_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("artifact_forbidden_imports", [])):
                    findings.append(
                        Finding(relative, line, "SWARCH015", f"artifact package imports runtime authority {imported}")
                    )
                if not _under(relative, config.get("artifact_service_roots", [])) and _matches_prefix(
                    imported, config.get("artifact_non_service_forbidden_imports", [])
                ):
                    findings.append(
                        Finding(relative, line, "SWARCH016", f"artifact primitive imports service authority {imported}")
                    )

        if relative.startswith("seasonalweather/") and not _under(
            relative,
            config.get("artifact_authority_allowed_roots", []),
        ):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("artifact_authority_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH017",
                            f"runtime module imports artifact publication authority {imported}",
                        )
                    )

        if _under(relative, config["script_roots"]):
            for imported, line in imports:
                if _matches_prefix(imported, config["script_forbidden_imports"]):
                    findings.append(
                        Finding(relative, line, "SWARCH005", f"script duplicates application authority via {imported}")
                    )

        if is_controller or is_worker or _under(relative, config["script_roots"]):
            for node in ast.walk(tree):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    call = _qualified_call(node.value)
                    if call in {"asyncio.create_task", "asyncio.ensure_future"}:
                        findings.append(
                            Finding(relative, node.lineno, "SWARCH006", f"unmanaged background task via {call}")
                        )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            lowered = node.value.lower()
            for term in config["compatibility_default_terms"]:
                if term in lowered and relative.startswith("seasonalweather/"):
                    findings.append(
                        Finding(relative, node.lineno, "SWARCH007", f"compatibility default references {term!r}")
                    )
            if (
                relative.startswith("seasonalweather/")
                and relative not in config.get("diagnostic_code_authorities", [])
                and re.fullmatch(r"SW[A-Z]+[0-9]{4}", node.value)
            ):
                findings.append(
                    Finding(
                        relative,
                        node.lineno,
                        "SWARCH021",
                        "permanent diagnostic code literal is outside the reviewed binding authority",
                    )
                )
            if (
                relative.startswith("seasonalweather/")
                and relative
                not in {
                    config.get("diagnostics_resource_loader"),
                    *config.get("diagnostics_file_authorities", []),
                }
                and node.value in {"catalog.json", "source.json", "catalog/catalog.json"}
            ):
                findings.append(
                    Finding(
                        relative,
                        node.lineno,
                        "SWARCH022",
                        "diagnostic catalog file access bypasses its owned loader/compiler",
                    )
                )
            if relative.startswith("seasonalweather/") and "/var/lib" in lowered and "diagnostic" in lowered:
                findings.append(
                    Finding(
                        relative,
                        node.lineno,
                        "SWARCH024",
                        "canonical diagnostic catalog cannot use mutable /var/lib authority",
                    )
                )

    for script_root in config["script_roots"]:
        directory = root / script_root
        if not directory.is_dir():
            continue
        for path in sorted(candidate for candidate in directory.rglob("*") if candidate.is_file()):
            if path.suffix == ".py":
                continue
            relative = path.relative_to(root).as_posix()
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                lowered = line.lower()
                for term in config["script_forbidden_shell_terms"]:
                    if term.lower() in lowered:
                        findings.append(
                            Finding(
                                relative,
                                line_number,
                                "SWARCH005",
                                f"script duplicates application authority via {term!r}",
                            )
                        )

    return sorted(finding for finding in findings if not _exception_applies(finding, exceptions))


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce SeasonalWeather architecture ownership.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path, default=ROOT / "quality/architecture.toml")
    parser.add_argument("--no-exceptions", action="store_true")
    args = parser.parse_args()

    config = load_toml(args.config)
    exceptions = []
    if not args.no_exceptions and args.root.resolve() == ROOT:
        exceptions = load_toml(ROOT / "quality/exceptions.toml").get("exceptions", [])
    raw_findings = scan(args.root.resolve(), config)
    unused_exceptions = [
        item for item in exceptions if not any(_exception_applies(finding, [item]) for finding in raw_findings)
    ]
    findings = [finding for finding in raw_findings if not _exception_applies(finding, exceptions)]
    for finding in findings:
        print(finding.render())
    for item in unused_exceptions:
        print(
            "quality/exceptions.toml: "
            f"SWARCH998 unused exception for {item.get('rule')} at {item.get('scope')}; remove stale exceptions"
        )
    if findings or unused_exceptions:
        print(f"architecture-check: {len(findings)} violation(s), {len(unused_exceptions)} stale exception(s)")
        return 1
    print("architecture-check: ownership rules satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
