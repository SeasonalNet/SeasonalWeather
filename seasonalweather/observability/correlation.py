"""Bounded context propagation for logs and metrics-adjacent diagnostics."""

from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import final

_MAX_VALUE = 128
_FIELDS: ContextVar[dict[str, str] | None] = ContextVar("seasonalweather_correlation", default=None)
_ALLOWED = frozenset(
    {
        "service",
        "role",
        "build_id",
        "build_identity",
        "instance_id",
        "worker_id",
        "swwp_session",
        "command_id",
        "job_id",
        "lease_id",
        "request_id",
        "trace_id",
        "span_id",
        "configuration_generation",
        "diagnostic_code",
    }
)


def _value(value: object) -> str:
    text = str(value).strip()
    if not text or len(text) > _MAX_VALUE or any(ord(char) < 0x20 for char in text):
        raise ValueError("correlation values must be non-empty printable bounded text")
    return text


@final
@dataclass(frozen=True)
class CorrelationFields:
    """Immutable allowlisted correlation fields."""

    fields: tuple[tuple[str, str], ...] = ()

    def __getitem__(self, key: str) -> str:
        return dict(self.fields)[key]

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self.fields)

    def __len__(self) -> int:
        return len(self.fields)

    def as_dict(self) -> dict[str, str]:
        return dict(self.fields)


def _normalize(fields: Mapping[str, object]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, raw in fields.items():
        if key not in _ALLOWED:
            raise ValueError(f"unsupported correlation field: {key}")
        normalized[key] = _value(raw)
    return normalized


def current_correlation() -> CorrelationFields:
    return CorrelationFields(tuple(sorted((_FIELDS.get() or {}).items())))


def set_correlation(**fields: object) -> CorrelationFields:
    """Set process/task-local defaults and return the resulting immutable view."""

    normalized = _normalize(fields)
    current = dict(_FIELDS.get() or {})
    current.update(normalized)
    _ = _FIELDS.set(current)
    return current_correlation()


@contextmanager
def bind_correlation(**fields: object) -> Generator[CorrelationFields, None, None]:
    normalized = _normalize(fields)
    current = dict(_FIELDS.get() or {})
    current.update(normalized)
    token: Token[dict[str, str] | None] = _FIELDS.set(current)
    try:
        yield current_correlation()
    finally:
        _FIELDS.reset(token)
