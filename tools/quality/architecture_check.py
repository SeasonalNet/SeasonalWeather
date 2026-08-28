from __future__ import annotations

import argparse
import ast
import re
import tomllib
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
            resolved = _resolve_import(node, module)
            yield resolved, node.lineno
            if node.module is None:
                for alias in node.names:
                    if alias.name != "*":
                        yield f"{resolved}.{alias.name}", node.lineno


def _module_index(root: Path) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in sorted(root.rglob("*.py")):
        if any(part in {".git", ".venv", ".venv-ci", "__pycache__"} for part in path.parts):
            continue
        if path.relative_to(root).as_posix().startswith("tests/architecture/fixtures/"):
            continue
        modules[_module_name(path, root)] = path
    return modules


def _resolve_local_import(imported: str, modules: dict[str, Path]) -> str | None:
    candidate = imported
    while candidate:
        if candidate in modules:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return None


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[tuple[str, ...]]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for child in sorted(graph[node]):
            if child not in indices:
                visit(child)
                lowlinks[node] = min(lowlinks[node], lowlinks[child])
            elif child in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[child])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            child = stack.pop()
            on_stack.remove(child)
            component.append(child)
            if child == node:
                break
        components.append(tuple(sorted(component)))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return components


def _import_cycle_findings(root: Path, config: dict[str, Any]) -> list[Finding]:
    module_index = _module_index(root)
    cycle_roots = config.get("import_cycle_roots", [])
    modules = set(module_index)
    graph: dict[str, set[str]] = {module: set() for module in modules}
    import_lines: dict[tuple[str, str], int] = {}
    for module in sorted(modules):
        module_path = module_index[module]
        try:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=module_path.as_posix())
        except (OSError, SyntaxError):
            continue
        for imported, line in _imports(tree, module):
            target = _resolve_local_import(imported, module_index)
            if target is None or target not in modules:
                continue
            graph[module].add(target)
            import_lines.setdefault((module, target), line)

    findings: list[Finding] = []
    for component in _strongly_connected_components(graph):
        component_set = set(component)
        cyclic = len(component) > 1 and any(_matches_prefix(node, cycle_roots) for node in component)
        if not cyclic or not component:
            continue
        cycle_text = " -> ".join((*component, component[0]))
        for module in component:
            targets = sorted(graph[module] & component_set)
            if not targets:
                continue
            target = targets[0]
            relative_path = module_index[module].relative_to(root).as_posix()
            findings.append(
                Finding(
                    relative_path,
                    import_lines[(module, target)],
                    "SWARCH050",
                    f"import cycle detected: {cycle_text}",
                )
            )
    return findings


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


def _module_literal_collections(tree: ast.Module, canonical_keys: set[str]) -> tuple[int, set[str]]:
    """Find module-level literal collections that repeat canonical segment keys."""

    def literal_collection(value: ast.expr | None) -> ast.expr | None:
        if isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            return value
        if (
            isinstance(value, ast.Call)
            and _qualified_call(value) in {"frozenset", "set", "tuple", "list"}
            and len(value.args) == 1
            and not value.keywords
        ):
            wrapped = value.args[0]
            if isinstance(wrapped, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                return wrapped
        return None

    first_line = 0
    collected: set[str] = set()
    for statement in tree.body:
        value: ast.expr | None = None
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
        value = literal_collection(value)
        if value is None:
            continue
        literals = {
            node.value for node in ast.walk(value) if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        matching = literals & canonical_keys
        if matching:
            if first_line == 0:
                first_line = statement.lineno
            collected.update(matching)
    return first_line, collected


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


_JSON_SERIALIZERS = {"json.dump", "json.dumps"}
_JSON_PATH_FACTORY_NAMES = {"_journal_path", "_receipt_path"}
_JSON_WRITE_METHODS = {"dump", "persist", "save", "store", "write", "write_bytes", "write_text"}
_JSON_FILE_MUTATIONS = {
    "os.open",
    "os.rename",
    "os.replace",
    "Path.replace",
    "pathlib.Path.replace",
}
_JSON_SHELL_MUTATION = re.compile(
    r"(?:>>?|\b(?:cp|install|mv)\b)[^#\n]*\.json\b|\.json\b[^#\n]*(?:>>?|\b(?:cp|install|mv)\b)", re.IGNORECASE
)


def _symbol(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _symbol(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return None


def _string_literals(node: ast.AST) -> Iterable[str]:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value
        elif isinstance(child, ast.JoinedStr):
            for value in child.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    yield value.value


def _looks_like_json_path(node: ast.AST, symbols: set[str], path_factories: set[str]) -> bool:
    symbol = _symbol(node)
    if symbol is not None and (symbol in symbols or symbol.rpartition(".")[2] in symbols):
        return True
    if any(value.casefold().endswith(".json") or ".json" in value.casefold() for value in _string_literals(node)):
        return True
    if isinstance(node, ast.Call):
        call = _qualified_call(node)
        if call.rpartition(".")[2] in path_factories:
            return True
        if call.rpartition(".")[2] in {"with_name", "with_suffix"}:
            return any(_looks_like_json_path(argument, symbols, path_factories) for argument in node.args)
        if call in {"Path", "pathlib.Path"}:
            return any(_looks_like_json_path(argument, symbols, path_factories) for argument in node.args)
        return False
    if isinstance(node, ast.BinOp):
        return _looks_like_json_path(node.left, symbols, path_factories) or _looks_like_json_path(
            node.right, symbols, path_factories
        )
    if isinstance(node, ast.IfExp):
        return any(_looks_like_json_path(child, symbols, path_factories) for child in (node.body, node.orelse))
    return False


def _json_path_symbols(tree: ast.AST, path_factories: set[str]) -> set[str]:
    symbols: set[str] = set()
    changed = True
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))]
    while changed:
        changed = False
        for node in assignments:
            targets: list[ast.expr]
            value: ast.expr | None
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            else:
                targets = [node.target]
                value = node.value
            if value is None or not _looks_like_json_path(value, symbols, path_factories):
                continue
            for target in targets:
                target_symbol = _symbol(target)
                if target_symbol is not None and target_symbol not in symbols:
                    symbols.add(target_symbol)
                    changed = True
    return symbols


def _open_mode(node: ast.Call) -> str | None:
    mode: object = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            mode = keyword.value.value
    return mode if isinstance(mode, str) else None


def _os_open_is_read_only(node: ast.Call) -> bool:
    if len(node.args) < 2:
        return False
    flags = ast.unparse(node.args[1])
    return "O_RDONLY" in flags and not any(token in flags for token in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC"))


def _json_file_mutation(node: ast.Call, symbols: set[str], path_factories: set[str]) -> str | None:
    call = _qualified_call(node)
    if call == "open" or (isinstance(node.func, ast.Attribute) and node.func.attr == "open"):
        path = (
            node.args[0]
            if node.args
            else next((keyword.value for keyword in node.keywords if keyword.arg in {"file", "path"}), None)
        )
        mode = _open_mode(node)
        if (
            path is not None
            and mode is not None
            and any(flag in mode for flag in "wax+")
            and _looks_like_json_path(path, symbols, path_factories)
        ):
            return call
        return None
    if call == "os.open":
        path = node.args[0] if node.args else None
        if (
            path is not None
            and not _os_open_is_read_only(node)
            and _looks_like_json_path(path, symbols, path_factories)
        ):
            return call
        return None
    if call in {"os.replace", "os.rename"}:
        path = node.args[1] if len(node.args) > 1 else None
        if path is not None and _looks_like_json_path(path, symbols, path_factories):
            return call
        return None
    if isinstance(node.func, ast.Attribute):
        owner = node.func.value
        if node.func.attr in {"write_bytes", "write_text", "replace"}:
            if _looks_like_json_path(owner, symbols, path_factories):
                return f"{_symbol(owner) or 'path'}.{node.func.attr}"
            return None
        if (
            node.func.attr in _JSON_WRITE_METHODS
            and node.args
            and _looks_like_json_path(node.args[0], symbols, path_factories)
        ):
            return call
    return None


def _filesystem_write_call(node: ast.Call) -> bool:
    call = _qualified_call(node)
    if call in _JSON_FILE_MUTATIONS or call in {"os.fdopen", "os.write", "Path.write_bytes", "Path.write_text"}:
        return True
    if call == "open" or (isinstance(node.func, ast.Attribute) and node.func.attr == "open"):
        mode = _open_mode(node)
        return mode is not None and any(flag in mode for flag in "wax+")
    if call == "os.open":
        return not _os_open_is_read_only(node)
    return isinstance(node.func, ast.Attribute) and node.func.attr in {"write_bytes", "write_text"}


def _json_persistence_callable(module: str, classes: list[str], functions: list[str]) -> str:
    owner = ".".join([*classes, *functions])
    return f"{module}:{owner}" if owner else module


class _JsonPersistenceVisitor(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.classes: list[str] = []
        self.functions: list[str] = []
        self.calls: dict[str, list[ast.Call]] = {}

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def visit_Call(self, node: ast.Call) -> None:
        callable_name = _json_persistence_callable(self.module, self.classes, self.functions)
        self.calls.setdefault(callable_name, []).append(node)
        self.generic_visit(node)


def _json_persistence_findings(
    relative: str,
    module: str,
    tree: ast.AST,
    config: dict[str, Any],
) -> list[Finding]:
    roots = config.get("json_persistence_roots", ["seasonalweather"])
    if not _under(relative, roots):
        return []
    symbols = _json_path_symbols(tree, set(config.get("json_persistence_path_factories", _JSON_PATH_FACTORY_NAMES)))
    visitor = _JsonPersistenceVisitor(module)
    visitor.visit(tree)
    allowed = set(config.get("json_persistence_allowed_callables", []))
    path_factories = set(config.get("json_persistence_path_factories", _JSON_PATH_FACTORY_NAMES))
    findings: list[Finding] = []
    for callable_name, calls in visitor.calls.items():
        if callable_name in allowed:
            continue
        direct = [
            (node, mutation)
            for node in calls
            if (mutation := _json_file_mutation(node, symbols, path_factories)) is not None
        ]
        if direct:
            findings.extend(
                Finding(
                    relative,
                    node.lineno,
                    "SWARCH057",
                    f"JSON file persistence must use the database authority; filesystem mutation via {mutation}",
                )
                for node, mutation in direct
            )
            continue
        serializers = [node for node in calls if _qualified_call(node) in _JSON_SERIALIZERS]
        mutations = [node for node in calls if _filesystem_write_call(node)]
        if serializers and mutations:
            findings.append(
                Finding(
                    relative,
                    mutations[0].lineno,
                    "SWARCH057",
                    "JSON serialization and filesystem writing must not form an unapproved persistence path",
                )
            )
    return findings


def _under(path: str, roots: Iterable[str]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


def _exception_applies(finding: Finding, exceptions: list[dict[str, Any]]) -> bool:
    return any(
        item.get("rule") == finding.rule
        and (finding.path == item.get("scope") or finding.path.startswith(f"{item.get('scope', '')}/"))
        for item in exceptions
    )


def _dependency_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", value)
    if match is None:
        return None
    return match.group(1).replace("_", "-").lower()


def _metadata_groups(document: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        groups.update(optional)
    dependency_groups = document.get("dependency-groups")
    if isinstance(dependency_groups, dict):
        groups.update(dependency_groups)
    return groups


def _metadata_dependency_names(values: object) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {name for value in values if (name := _dependency_name(value)) is not None}


def _project_metadata_findings(root: Path, config: dict[str, Any]) -> list[Finding]:
    metadata_path = root / str(config.get("project_metadata_path", ""))
    if not metadata_path.is_file():
        return []
    try:
        document = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []

    project = document.get("project")
    if not isinstance(project, dict):
        return []
    groups = _metadata_groups(document, project)
    if not groups:
        return []

    controller_group = str(config.get("controller_dependency_group", "controller"))
    controller_names = _metadata_dependency_names(groups.get(controller_group, []))
    forbidden_controller_names = _metadata_dependency_names(config.get("controller_forbidden_worker_dependencies", []))
    findings: list[Finding] = []
    if controller_names & forbidden_controller_names:
        findings.append(
            Finding(
                metadata_path.relative_to(root).as_posix(),
                1,
                "SWARCH052",
                "controller dependency section contains worker-only dependency: "
                + ", ".join(sorted(controller_names & forbidden_controller_names)),
            )
        )

    base_names = _metadata_dependency_names(project.get("dependencies", []))
    base_overlap = base_names & controller_names
    if base_overlap:
        findings.append(
            Finding(
                metadata_path.relative_to(root).as_posix(),
                1,
                "SWARCH053",
                "worker-safe project dependencies contain controller dependency: " + ", ".join(sorted(base_overlap)),
            )
        )

    for group in config.get("worker_dependency_groups", []):
        worker_names = _metadata_dependency_names(groups.get(str(group), []))
        overlap = worker_names & controller_names
        if overlap:
            findings.append(
                Finding(
                    metadata_path.relative_to(root).as_posix(),
                    1,
                    "SWARCH053",
                    f"worker dependency section {group!r} contains controller dependency: "
                    + ", ".join(sorted(overlap)),
                )
            )
    return findings


def scan(root: Path, config: dict[str, Any], exceptions: list[dict[str, Any]] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    exceptions = exceptions or []
    findings.extend(_project_metadata_findings(root, config))
    findings.extend(_import_cycle_findings(root, config))
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
        findings.extend(_json_persistence_findings(relative, module, tree, config))

        if _under(relative, config.get("segment_api_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("segment_api_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH046",
                            f"segment API route imports runtime mutation authority {imported}",
                        )
                    )

        if _under(relative, config.get("segment_builder_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("segment_builder_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH047",
                            f"independent segment builder imports artifact/runtime authority {imported}",
                        )
                    )
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _qualified_call(node) in config.get(
                    "segment_builder_forbidden_calls", []
                ):
                    findings.append(
                        Finding(
                            relative,
                            node.lineno,
                            "SWARCH047",
                            "independent segment builder cannot promote or publish artifacts",
                        )
                    )

        if _under(relative, config.get("segment_service_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("segment_service_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH048",
                            f"segment application service imports forbidden runtime owner {imported}",
                        )
                    )

        if relative == "seasonalweather/control.py":
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _qualified_call(node) in config.get(
                    "segment_control_forbidden_calls", []
                ):
                    findings.append(
                        Finding(
                            relative,
                            node.lineno,
                            "SWARCH049",
                            "control facade cannot compose the segment application dependency graph",
                        )
                    )

        segment_owner_roots = config.get("segment_registry_owner_roots", [])
        if _under(relative, config.get("segment_policy_consumer_roots", [])) and not _under(
            relative, segment_owner_roots
        ):
            source = path.read_text(encoding="utf-8")
            for term in config.get("segment_policy_forbidden_terms", []):
                if term in source:
                    line = next(
                        (index for index, value in enumerate(source.splitlines(), start=1) if term in value),
                        1,
                    )
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH044",
                            f"segment policy authority must be queried from the registry: {term!r}",
                        )
                    )
            static_line, static_keys = _module_literal_collections(
                tree,
                set(config.get("segment_registry_canonical_keys", [])),
            )
            if len(static_keys) >= 3:
                findings.append(
                    Finding(
                        relative,
                        static_line,
                        "SWARCH044",
                        "module-level literal collections repeat canonical static segment authority",
                    )
                )
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _qualified_call(node) in config.get(
                    "segment_policy_forbidden_calls", []
                ):
                    findings.append(
                        Finding(
                            relative,
                            node.lineno,
                            "SWARCH044",
                            "segment policy definitions must be constructed only by the registry owner",
                        )
                    )

        if _under(relative, segment_owner_roots):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("segment_registry_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH045",
                            f"segment registry imports forbidden runtime or mutation authority {imported}",
                        )
                    )
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
                            "SWARCH045",
                            f"segment registry parses configuration directly via {call}",
                        )
                    )
                mutation = _filesystem_mutation(node, path_variables)
                if mutation:
                    findings.append(
                        Finding(
                            relative,
                            node.lineno,
                            "SWARCH045",
                            f"segment registry performs filesystem mutation via {mutation}",
                        )
                    )

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

        if is_controller and not _under(relative, config.get("formatter_owner_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("formatter_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH055",
                            f"production prose formatting must use {config['formatter_subsystem_root']}: {imported}",
                        )
                    )

        if _under(relative, config.get("formatter_compatibility_roots", [])):
            allowed_shim_nodes = (ast.Expr, ast.Import, ast.ImportFrom)
            for node in tree.body:
                if isinstance(node, allowed_shim_nodes):
                    continue
                if isinstance(node, ast.Assign) and all(
                    isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
                ):
                    continue
                findings.append(
                    Finding(
                        relative,
                        node.lineno,
                        "SWARCH056",
                        f"formatter compatibility module must only re-export from {config['formatter_subsystem_root']}",
                    )
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

        if _under(relative, config.get("observability_roots", [])):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("observability_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH051",
                            f"observability imports controller, worker, or persistence authority {imported}",
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

        if is_controller and not _under(relative, ["seasonalweather/tts"]):
            for imported, line in imports:
                if _matches_prefix(imported, config.get("tts_controller_forbidden_imports", [])):
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH054",
                            f"controller cannot import local TTS execution authority {imported}",
                        )
                    )
            source = path.read_text(encoding="utf-8")
            for term in config.get("tts_controller_forbidden_source_terms", []):
                if term in source:
                    line = next(
                        (index for index, value in enumerate(source.splitlines(), start=1) if term in value),
                        1,
                    )
                    findings.append(
                        Finding(
                            relative,
                            line,
                            "SWARCH054",
                            f"controller cannot retain local TTS authority {term!r}",
                        )
                    )
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and (
                    _qualified_call(node) in config.get("tts_controller_forbidden_call_attributes", [])
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr in config.get("tts_controller_forbidden_call_attributes", [])
                    )
                ):
                    findings.append(
                        Finding(
                            relative,
                            node.lineno,
                            "SWARCH054",
                            "controller cannot execute local TTS synthesis; submit a worker job",
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
                elif (
                    _matches_prefix(imported, config.get("nwws_forbidden_imports", []))
                    and _under(
                        relative,
                        config.get("nwws_adapter_roots", []),
                    )
                    and imported != "slixmpp"
                ):
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
                if relative not in config.get("json_persistence_allowed_scripts", []) and _JSON_SHELL_MUTATION.search(
                    line
                ):
                    findings.append(
                        Finding(
                            relative,
                            line_number,
                            "SWARCH057",
                            "JSON file persistence must use the database authority; shell filesystem mutation detected",
                        )
                    )
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
