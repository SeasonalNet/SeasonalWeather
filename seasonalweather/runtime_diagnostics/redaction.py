"""Small deterministic redaction and text-bound policy."""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer|token|password|secret|credential|api[_-]?key)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\b(?:sw[ac])_[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{3,}\b"),
    re.compile(r"(?i)SENTINEL[-_A-Z0-9]*SECRET[-_A-Z0-9]*"),
)


def redact_text(value: object, *, limit: int) -> str:
    text = value if isinstance(value, str) else type(value).__name__
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    text = "".join(character if character.isprintable() or character in "\n\t" else "\ufffd" for character in text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"
