"""Safe source-preserving YAML 1.2-core parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml
from yaml.composer import ComposerError
from yaml.error import Mark, MarkedYAMLError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.resolver import BaseResolver

from .issues import CompileIssue, IssuePhase
from .paths import ROOT_PATH, ConfigPath
from .redaction import is_secret_path, line_looks_secret
from .source import (
    DEFAULT_LIMITS,
    CompilerLimits,
    NodeLocations,
    ParsedSource,
    RelatedLocation,
    SourceDocument,
    SourceLocation,
    SourcePosition,
    SourceSpan,
)

_BOOL_RE = re.compile(r"^(?:true|false)$", re.IGNORECASE)
_NULL_RE = re.compile(r"^(?:~|null|Null|NULL)?$")
_INT_RE = re.compile(r"^(?:[-+]?(?:0|[1-9][0-9_]*|0b[0-1_]+|0o[0-7_]+|0x[0-9a-fA-F_]+))$")
_FLOAT_RE = re.compile(
    r"""^(?:[-+]?(?:
        (?:[0-9][0-9_]*)?\.[0-9_]+(?:[eE][-+]?[0-9]+)?
        |[0-9][0-9_]*(?:[eE][-+]?[0-9]+)
        |\.inf|\.Inf|\.INF|\.nan|\.NaN|\.NAN
    ))$""",
    re.X,
)
_ALLOWED_TAGS = frozenset(
    {
        "tag:yaml.org,2002:map",
        "tag:yaml.org,2002:seq",
        "tag:yaml.org,2002:str",
        "tag:yaml.org,2002:null",
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:float",
    }
)


class RestrictedSafeLoader(yaml.SafeLoader):
    """Safe loader with explicit YAML 1.2 core scalar resolution."""

    yaml_implicit_resolvers = {key: list(value) for key, value in yaml.SafeLoader.yaml_implicit_resolvers.items()}

    def compose_node(self, parent: Node | None, index: object) -> Node:
        if self.check_event(AliasEvent):
            event = self.get_event()
            raise RestrictedYamlError(
                "yaml.alias",
                "YAML aliases are not supported in configuration.",
                event.start_mark,
            )
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise RestrictedYamlError(
                "yaml.anchor",
                "YAML anchors are not supported in configuration.",
                event.start_mark,
            )
        return super().compose_node(parent, index)


for first_character, resolvers in tuple(RestrictedSafeLoader.yaml_implicit_resolvers.items()):
    RestrictedSafeLoader.yaml_implicit_resolvers[first_character] = [
        resolver
        for resolver in resolvers
        if resolver[0]
        not in {
            "tag:yaml.org,2002:bool",
            "tag:yaml.org,2002:float",
            "tag:yaml.org,2002:int",
            "tag:yaml.org,2002:null",
            "tag:yaml.org,2002:timestamp",
        }
    ]
RestrictedSafeLoader.add_implicit_resolver("tag:yaml.org,2002:bool", _BOOL_RE, list("tTfF"))
RestrictedSafeLoader.add_implicit_resolver("tag:yaml.org,2002:null", _NULL_RE, ["~", "n", "N", ""])
RestrictedSafeLoader.add_implicit_resolver("tag:yaml.org,2002:int", _INT_RE, list("-+0123456789"))
RestrictedSafeLoader.add_implicit_resolver("tag:yaml.org,2002:float", _FLOAT_RE, list("-+0123456789."))


class RestrictedYamlError(Exception):
    def __init__(self, rule_id: str, message: str, mark: Mark | None) -> None:
        self.rule_id = rule_id
        self.safe_message = message
        self.mark = mark
        super().__init__(message)


@dataclass(frozen=True)
class ParseOutcome:
    parsed: ParsedSource | None
    issues: tuple[CompileIssue, ...]


def parse_document(
    document: SourceDocument,
    *,
    limits: CompilerLimits = DEFAULT_LIMITS,
) -> ParseOutcome:
    loader = RestrictedSafeLoader(document.text)
    try:
        root = loader.get_single_node()
        if root is None:
            return ParseOutcome(
                None,
                (
                    _issue(
                        document,
                        "yaml.empty",
                        "Configuration source is empty.",
                        None,
                    ),
                ),
            )
        locations: dict[ConfigPath, NodeLocations] = {}
        issues: list[CompileIssue] = []
        state = _TraversalState(limits=limits)
        _inspect_node(
            root,
            document=document,
            path=ROOT_PATH,
            depth=0,
            locations=locations,
            issues=issues,
            state=state,
        )
        issues = _bounded_issues(issues, document, limits)
        if issues:
            return ParseOutcome(None, tuple(sorted(issues, key=CompileIssue.sort_key)))
        try:
            value = loader.construct_document(root)
        except (OverflowError, ValueError):
            return ParseOutcome(
                None,
                (
                    CompileIssue(
                        rule_id="yaml.scalar_construction",
                        phase=IssuePhase.PARSE,
                        message=("A YAML scalar cannot be represented safely under the configured core semantics."),
                        path=ROOT_PATH,
                        primary=_location(
                            document.source_id,
                            root.start_mark,
                            root.end_mark,
                        ),
                        redacted=True,
                    ),
                ),
            )
        if not isinstance(value, dict):
            location = _location(document.source_id, root.start_mark, root.end_mark)
            return ParseOutcome(
                None,
                (
                    CompileIssue(
                        rule_id="yaml.root_mapping",
                        phase=IssuePhase.PARSE,
                        message="Configuration root must be a mapping.",
                        path=ROOT_PATH,
                        primary=location,
                    ),
                ),
            )
        document_location = _location(
            document.source_id,
            root.start_mark,
            root.end_mark,
            label="configuration document",
        )
        return ParseOutcome(
            ParsedSource(
                value=value,
                locations=locations,
                document_location=document_location,
            ),
            (),
        )
    except RestrictedYamlError as exc:
        return ParseOutcome(
            None,
            (
                _issue(
                    document,
                    exc.rule_id,
                    exc.safe_message,
                    exc.mark,
                ),
            ),
        )
    except ComposerError as exc:
        return ParseOutcome(
            None,
            (
                _issue(
                    document,
                    "yaml.multiple_documents",
                    "Only one YAML document is supported.",
                    exc.problem_mark or exc.context_mark,
                ),
            ),
        )
    except MarkedYAMLError as exc:
        return ParseOutcome(
            None,
            (
                _issue(
                    document,
                    "yaml.syntax",
                    "Configuration contains malformed YAML syntax.",
                    exc.problem_mark or exc.context_mark,
                ),
            ),
        )
    finally:
        loader.dispose()


@dataclass
class _TraversalState:
    limits: CompilerLimits
    nodes: int = 0


def _inspect_node(
    node: Node,
    *,
    document: SourceDocument,
    path: ConfigPath,
    depth: int,
    locations: dict[ConfigPath, NodeLocations],
    issues: list[CompileIssue],
    state: _TraversalState,
) -> None:
    if len(issues) > state.limits.max_issues:
        return
    state.nodes += 1
    if _node_is_rejected(
        node,
        document=document,
        path=path,
        depth=depth,
        issues=issues,
        state=state,
    ):
        return
    if isinstance(node, ScalarNode):
        _inspect_scalar(
            node,
            document=document,
            path=path,
            locations=locations,
            issues=issues,
            state=state,
        )
        return
    if isinstance(node, SequenceNode):
        _inspect_sequence(
            node,
            document=document,
            path=path,
            depth=depth,
            locations=locations,
            issues=issues,
            state=state,
        )
        return
    if isinstance(node, MappingNode):
        _inspect_mapping(
            node,
            document=document,
            path=path,
            depth=depth,
            locations=locations,
            issues=issues,
            state=state,
        )


def _node_is_rejected(
    node: Node,
    *,
    document: SourceDocument,
    path: ConfigPath,
    depth: int,
    issues: list[CompileIssue],
    state: _TraversalState,
) -> bool:
    limit: tuple[str, str] | None = None
    if state.nodes > state.limits.max_nodes:
        limit = ("source.limit.nodes", "Configuration exceeds the node limit.")
    elif depth > state.limits.max_depth:
        limit = (
            "source.limit.depth",
            "Configuration exceeds the nesting-depth limit.",
        )
    if limit is not None:
        _append_limit_issue(issues, document, node, limit[0], limit[1], path)
        return True
    if node.tag in _ALLOWED_TAGS:
        return False
    merge = node.tag == "tag:yaml.org,2002:merge"
    issues.append(
        CompileIssue(
            rule_id="yaml.merge_key" if merge else "yaml.tag",
            phase=IssuePhase.PARSE,
            message=(
                "YAML merge keys are not supported in configuration."
                if merge
                else "Custom YAML tags are not supported in configuration."
            ),
            path=path,
            primary=_location(document.source_id, node.start_mark, node.end_mark),
            redacted=is_secret_path(path),
        )
    )
    return True


def _inspect_scalar(
    node: ScalarNode,
    *,
    document: SourceDocument,
    path: ConfigPath,
    locations: dict[ConfigPath, NodeLocations],
    issues: list[CompileIssue],
    state: _TraversalState,
) -> None:
    if len(node.value) > state.limits.max_scalar_codepoints:
        _append_limit_issue(
            issues,
            document,
            node,
            "source.limit.scalar",
            "Configuration scalar exceeds the length limit.",
            path,
        )
    location = _location(document.source_id, node.start_mark, node.end_mark)
    locations.setdefault(
        path,
        NodeLocations(key=None, value=location, node=location),
    )


def _inspect_sequence(
    node: SequenceNode,
    *,
    document: SourceDocument,
    path: ConfigPath,
    depth: int,
    locations: dict[ConfigPath, NodeLocations],
    issues: list[CompileIssue],
    state: _TraversalState,
) -> None:
    if len(node.value) > state.limits.max_collection_items:
        _append_limit_issue(
            issues,
            document,
            node,
            "source.limit.collection",
            "Configuration sequence exceeds the item limit.",
            path,
        )
        return
    location = _location(document.source_id, node.start_mark, node.end_mark)
    locations.setdefault(
        path,
        NodeLocations(key=None, value=location, node=location),
    )
    for index, child in enumerate(node.value):
        if len(issues) > state.limits.max_issues:
            break
        child_path = path.index(index)
        child_location = _location(
            document.source_id,
            child.start_mark,
            child.end_mark,
            label="sequence item",
        )
        locations[child_path] = NodeLocations(
            key=None,
            value=child_location,
            node=child_location,
        )
        _inspect_node(
            child,
            document=document,
            path=child_path,
            depth=depth + 1,
            locations=locations,
            issues=issues,
            state=state,
        )


def _inspect_mapping(
    node: MappingNode,
    *,
    document: SourceDocument,
    path: ConfigPath,
    depth: int,
    locations: dict[ConfigPath, NodeLocations],
    issues: list[CompileIssue],
    state: _TraversalState,
) -> None:
    if len(node.value) > state.limits.max_collection_items:
        _append_limit_issue(
            issues,
            document,
            node,
            "source.limit.collection",
            "Configuration mapping exceeds the item limit.",
            path,
        )
        return
    location = _location(document.source_id, node.start_mark, node.end_mark)
    locations.setdefault(
        path,
        NodeLocations(key=None, value=location, node=location),
    )
    first_keys: dict[str, ScalarNode] = {}
    for key_node, value_node in node.value:
        if len(issues) > state.limits.max_issues:
            break
        if not isinstance(key_node, ScalarNode) or key_node.tag != BaseResolver.DEFAULT_SCALAR_TAG:
            issues.append(
                CompileIssue(
                    rule_id="yaml.non_string_key",
                    phase=IssuePhase.PARSE,
                    message="Configuration mapping keys must be strings.",
                    path=path,
                    primary=_location(document.source_id, key_node.start_mark, key_node.end_mark),
                )
            )
            continue
        child_path = _record_mapping_entry(
            key_node,
            value_node,
            document=document,
            path=path,
            first_keys=first_keys,
            locations=locations,
            issues=issues,
        )
        _inspect_node(
            value_node,
            document=document,
            path=child_path,
            depth=depth + 1,
            locations=locations,
            issues=issues,
            state=state,
        )


def _record_mapping_entry(
    key_node: ScalarNode,
    value_node: Node,
    *,
    document: SourceDocument,
    path: ConfigPath,
    first_keys: dict[str, ScalarNode],
    locations: dict[ConfigPath, NodeLocations],
    issues: list[CompileIssue],
) -> ConfigPath:
    key = key_node.value
    child_path = path.field(key)
    key_location = _location(document.source_id, key_node.start_mark, key_node.end_mark, label="key")
    value_location = _location(
        document.source_id,
        value_node.start_mark,
        value_node.end_mark,
        label="value",
    )
    locations[child_path] = NodeLocations(
        key=key_location,
        value=value_location,
        node=SourceLocation(
            source_id=document.source_id,
            span=SourceSpan(key_location.span.start, value_location.span.end),
            label="mapping entry",
        ),
    )
    first = first_keys.get(key)
    if first is not None:
        issues.append(
            CompileIssue(
                rule_id="yaml.duplicate_key",
                phase=IssuePhase.PARSE,
                message="Configuration mapping key is defined more than once.",
                path=child_path,
                primary=key_location,
                related=(
                    RelatedLocation(
                        _location(
                            document.source_id,
                            first.start_mark,
                            first.end_mark,
                            label="first definition",
                            role="related",
                        ),
                        "first_definition",
                    ),
                ),
                redacted=is_secret_path(child_path),
                help="Remove one of the duplicate key definitions.",
            )
        )
    else:
        first_keys[key] = key_node
    return child_path


def _append_limit_issue(
    issues: list[CompileIssue],
    document: SourceDocument,
    node: Node,
    rule_id: str,
    message: str,
    path: ConfigPath,
) -> None:
    if any(issue.rule_id == rule_id for issue in issues):
        return
    issues.append(
        CompileIssue(
            rule_id=rule_id,
            phase=IssuePhase.PARSE,
            message=message,
            path=path,
            primary=_location(document.source_id, node.start_mark, node.end_mark),
            redacted=is_secret_path(path),
        )
    )


def _bounded_issues(
    issues: list[CompileIssue],
    document: SourceDocument,
    limits: CompilerLimits,
) -> list[CompileIssue]:
    ordered = sorted(issues, key=CompileIssue.sort_key)
    if len(ordered) <= limits.max_issues:
        return ordered
    retained = ordered[: max(0, limits.max_issues - 1)]
    retained.append(
        CompileIssue(
            rule_id="compiler.issue_limit",
            phase=IssuePhase.PARSE,
            message="Additional configuration issues were omitted.",
            primary=SourceLocation(
                document.source_id,
                SourceSpan(SourcePosition(0, 0, 0), SourcePosition(0, 0, 0)),
            ),
        )
    )
    return retained


def _issue(
    document: SourceDocument,
    rule_id: str,
    message: str,
    mark: Mark | None,
) -> CompileIssue:
    location = _location(document.source_id, mark, mark) if mark is not None else None
    redacted = False
    if mark is not None:
        lines = document.lines()
        if 0 <= mark.line < len(lines):
            redacted = line_looks_secret(lines[mark.line])
    return CompileIssue(
        rule_id=rule_id,
        phase=IssuePhase.PARSE,
        message=message,
        primary=location,
        redacted=redacted,
    )


def _location(
    source_id: str,
    start: Mark,
    end: Mark,
    *,
    label: str = "",
    role: str = "primary",
) -> SourceLocation:
    return SourceLocation(
        source_id=source_id,
        span=SourceSpan(_position(start), _position(end)),
        label=label,
        role=role,
    )


def _position(mark: Mark) -> SourcePosition:
    return SourcePosition(line=mark.line, column=mark.column, offset=mark.index)
