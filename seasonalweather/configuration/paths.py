"""Canonical typed configuration paths."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import total_ordering
from typing import TypeAlias

PathSegment: TypeAlias = str | int


@total_ordering
@dataclass(frozen=True)
class ConfigPath:
    """A path made only from field names and sequence indexes."""

    segments: tuple[PathSegment, ...] = ()

    def field(self, name: str) -> ConfigPath:
        return ConfigPath((*self.segments, name))

    def index(self, index: int) -> ConfigPath:
        return ConfigPath((*self.segments, index))

    @property
    def parent(self) -> ConfigPath | None:
        return ConfigPath(self.segments[:-1]) if self.segments else None

    def to_pointer(self) -> str:
        if not self.segments:
            return ""
        return "/" + "/".join(str(segment).replace("~", "~0").replace("/", "~1") for segment in self.segments)

    def to_human(self) -> str:
        if not self.segments:
            return "<root>"
        result = ""
        for segment in self.segments:
            if isinstance(segment, int):
                result += f"[{segment}]"
            elif not result:
                result = _human_field(segment)
            else:
                rendered = _human_field(segment)
                result += f".{rendered}" if rendered == segment else f"[{rendered}]"
        return result

    def __iter__(self) -> Iterator[PathSegment]:
        return iter(self.segments)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ConfigPath):
            return NotImplemented
        return tuple(_sort_segment(item) for item in self.segments) < tuple(
            _sort_segment(item) for item in other.segments
        )

    def __str__(self) -> str:
        return self.to_human()


ROOT_PATH = ConfigPath()


def _human_field(value: str) -> str:
    if value.isidentifier():
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _sort_segment(value: PathSegment) -> tuple[int, str | int]:
    return (0, value) if isinstance(value, str) else (1, value)
