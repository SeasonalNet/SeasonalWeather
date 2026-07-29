"""Bounded immutable source documents and locations."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from .paths import ConfigPath


@dataclass(frozen=True)
class CompilerLimits:
    max_source_bytes: int = 1_048_576
    max_depth: int = 64
    max_nodes: int = 50_000
    max_collection_items: int = 10_000
    max_scalar_codepoints: int = 262_144
    max_aliases: int = 0
    max_issues: int = 100
    max_related_locations: int = 8
    frame_context_lines: int = 2
    frame_line_width: int = 160


DEFAULT_LIMITS = CompilerLimits()


@dataclass(frozen=True, order=True)
class SourcePosition:
    """Zero-based Unicode-code-point position."""

    line: int
    column: int
    offset: int | None = None

    def __post_init__(self) -> None:
        if self.line < 0 or self.column < 0 or (self.offset is not None and self.offset < 0):
            raise ValueError("source positions cannot be negative")


@dataclass(frozen=True)
class SourceSpan:
    """Half-open source span."""

    start: SourcePosition
    end: SourcePosition

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("source span end precedes its start")


@dataclass(frozen=True)
class SourceLocation:
    source_id: str
    span: SourceSpan
    label: str = ""
    role: str = "primary"

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source_id,
            "span": {
                "start": _position_dict(self.span.start),
                "end": _position_dict(self.span.end),
            },
            "label": self.label,
            "role": self.role,
        }


@dataclass(frozen=True)
class RelatedLocation:
    location: SourceLocation
    relationship: str

    def to_dict(self) -> dict[str, object]:
        return {
            "relationship": self.relationship,
            "location": self.location.to_dict(),
        }


@dataclass(frozen=True)
class NodeLocations:
    key: SourceLocation | None
    value: SourceLocation
    node: SourceLocation


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    text: str = field(repr=False)
    digest: str

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        source_id: str,
        limits: CompilerLimits = DEFAULT_LIMITS,
    ) -> SourceDocument:
        if len(data) > limits.max_source_bytes:
            raise SourceReadError(
                "source.limit.bytes",
                "Configuration source exceeds the byte limit.",
                source_id,
            )
        digest = sha256(data).hexdigest()
        content = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise SourceReadError(
                "source.encoding",
                "Configuration source is not valid UTF-8.",
                source_id,
            ) from exc
        return cls(source_id=source_id, text=text, digest=digest)

    @classmethod
    def read(
        cls,
        path: str | Path,
        *,
        limits: CompilerLimits = DEFAULT_LIMITS,
    ) -> SourceDocument:
        selected = Path(path)
        display = str(selected)
        try:
            with selected.open("rb") as handle:
                data = handle.read(limits.max_source_bytes + 1)
        except OSError as exc:
            raise SourceReadError(
                "source.read",
                "Configuration source could not be read.",
                display,
            ) from exc
        return cls.from_bytes(data, source_id=display, limits=limits)

    def lines(self) -> tuple[str, ...]:
        return tuple(self.text.splitlines())


@dataclass(frozen=True)
class ParsedSource:
    value: dict[str, object]
    locations: dict[ConfigPath, NodeLocations]
    document_location: SourceLocation


class SourceReadError(Exception):
    def __init__(self, rule_id: str, message: str, source_id: str) -> None:
        self.rule_id = rule_id
        self.safe_message = message
        self.source_id = source_id
        super().__init__(message)


def _position_dict(position: SourcePosition) -> dict[str, int]:
    result = {"line": position.line, "column": position.column}
    if position.offset is not None:
        result["offset"] = position.offset
    return result
