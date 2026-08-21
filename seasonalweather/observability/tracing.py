"""Small W3C trace-context implementation with strict bounds."""

from __future__ import annotations

import re
import secrets
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_TRACEPARENT = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_CURRENT: ContextVar[TraceContext | None] = ContextVar("seasonalweather_trace_context", default=None)


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    trace_flags: str = "01"
    version: str = "00"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", self.trace_id) or set(self.trace_id) == {"0"}:
            raise ValueError("trace_id must be a non-zero lowercase 128-bit value")
        if not re.fullmatch(r"[0-9a-f]{16}", self.span_id) or set(self.span_id) == {"0"}:
            raise ValueError("span_id must be a non-zero lowercase 64-bit value")
        if not re.fullmatch(r"[0-9a-f]{2}", self.trace_flags):
            raise ValueError("trace_flags must be one byte")
        if self.version != "00":
            raise ValueError("only W3C trace-context version 00 is supported")

    @classmethod
    def new(cls) -> TraceContext:
        return cls(secrets.token_hex(16), secrets.token_hex(8))

    @classmethod
    def parse(cls, value: str | None) -> TraceContext:
        if value is None:
            return cls.new()
        match = _TRACEPARENT.fullmatch(value.strip())
        if match is None or match.group("version") != "00":
            return cls.new()
        return cls(
            trace_id=match.group("trace"),
            span_id=match.group("span"),
            trace_flags=match.group("flags"),
            version=match.group("version"),
        )

    def child(self) -> TraceContext:
        return TraceContext(self.trace_id, secrets.token_hex(8), self.trace_flags, self.version)

    def as_traceparent(self) -> str:
        return f"{self.version}-{self.trace_id}-{self.span_id}-{self.trace_flags}"


def current_trace_context() -> TraceContext | None:
    return _CURRENT.get()


@contextmanager
def bind_trace_context(context: TraceContext) -> Generator[TraceContext, None, None]:
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)
