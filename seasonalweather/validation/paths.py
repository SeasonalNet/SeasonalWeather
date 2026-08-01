"""Bounded typed paths shared by validation and admission diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from seasonalweather.configuration.paths import ConfigPath

PathSegment = str | int


class PathKind(StrEnum):
    CONFIGURATION = "configuration"
    JSON_POINTER = "json_pointer"
    JOB_PAYLOAD = "job_payload"
    UPLOAD = "upload"
    SCHEDULED_INSERT = "scheduled_insert"
    AUTHENTICATION = "authentication"
    TTS = "tts"
    SEGMENT = "segment"
    IMPORT = "import"


def _validate_segment(segment: PathSegment) -> None:
    if isinstance(segment, bool) or not isinstance(segment, str | int):
        raise TypeError("diagnostic path segments must be strings or integers")
    if isinstance(segment, int) and segment < 0:
        raise ValueError("diagnostic path indexes must be nonnegative")
    if isinstance(segment, str) and (not segment or len(segment) > 128 or "\x00" in segment):
        raise ValueError("diagnostic path segment is empty, overlong, or unsafe")


@dataclass(frozen=True)
class DiagnosticPath:
    kind: PathKind
    segments: tuple[PathSegment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "segments", tuple(self.segments))
        if len(self.segments) > 64:
            raise ValueError("diagnostic path contains too many segments")
        for segment in self.segments:
            _validate_segment(segment)

    @classmethod
    def configuration(cls, path: ConfigPath) -> DiagnosticPath:
        return cls(PathKind.CONFIGURATION, path.segments)

    @classmethod
    def json_pointer(cls, pointer: str) -> DiagnosticPath:
        if pointer == "":
            return cls(PathKind.JSON_POINTER)
        if not pointer.startswith("/"):
            raise ValueError("JSON pointer must be empty or begin with '/'")
        segments: list[PathSegment] = []
        for token in pointer[1:].split("/"):
            decoded = token.replace("~1", "/").replace("~0", "~")
            if "~" in token.replace("~1", "").replace("~0", ""):
                raise ValueError("JSON pointer contains an invalid escape")
            segments.append(decoded)
        return cls(PathKind.JSON_POINTER, tuple(segments))

    def to_pointer(self) -> str:
        if not self.segments:
            return ""
        return "/" + "/".join(str(segment).replace("~", "~0").replace("/", "~1") for segment in self.segments)

    def to_human(self) -> str:
        if self.kind is PathKind.IMPORT and self.segments:
            return f"import:{'/'.join(str(item) for item in self.segments)}"
        output = ""
        for segment in self.segments:
            if isinstance(segment, int):
                output += f"[{segment}]"
            elif not output:
                output = segment
            else:
                output += f".{segment}"
        return output or "<root>"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "segments": list(self.segments),
            "pointer": self.to_pointer(),
            "human": self.to_human(),
        }

    def sort_key(self) -> tuple[object, ...]:
        return (
            self.kind.value,
            tuple((0, segment) if isinstance(segment, str) else (1, segment) for segment in self.segments),
        )
