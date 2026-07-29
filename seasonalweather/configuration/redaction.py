"""Schema and conservative pre-schema secret classification."""

from __future__ import annotations

import re

from .paths import ConfigPath

_SECRET_WORDS = frozenset(
    {
        "authorization",
        "credential",
        "credentials",
        "password",
        "private_key",
        "secret",
        "token",
        "tokens",
        "webhook",
    }
)
_KEY_PATTERN = re.compile(
    r"^\s*([A-Za-z0-9_.-]*(?:password|secret|token|credential|authorization|private[_-]?key|webhook)[A-Za-z0-9_.-]*)\s*:",
    re.IGNORECASE,
)


def is_secret_path(path: ConfigPath | None) -> bool:
    if path is None:
        return False
    for segment in path:
        if not isinstance(segment, str):
            continue
        normalized = segment.casefold().replace("-", "_")
        if normalized in _SECRET_WORDS or any(word in normalized for word in _SECRET_WORDS):
            return True
    return False


def line_looks_secret(line: str) -> bool:
    return bool(_KEY_PATTERN.match(line))


def redact_source_line(line: str) -> str:
    match = re.match(r"^(\s*[^:#\n]+:\s*).*$", line)
    if not match:
        return "<redacted>"
    return f"{match.group(1)}<redacted>"


def secret_source_lines(lines: tuple[str, ...]) -> frozenset[int]:
    """Conservatively identify secret declarations and multiline bodies."""
    secret_lines: set[int] = set()
    secret_indent: int | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if secret_indent is not None:
            if not stripped or indent > secret_indent:
                secret_lines.add(index)
                continue
            secret_indent = None
        if not line_looks_secret(line):
            continue
        secret_lines.add(index)
        _, _, value = line.partition(":")
        if value.strip().startswith(("|", ">")):
            secret_indent = indent
    return frozenset(secret_lines)
