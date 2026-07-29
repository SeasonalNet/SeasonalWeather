"""Conservative fatal boundary, emergency stderr, and faulthandler support."""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from seasonalweather.diagnostics.bindings import RUNTIME_CODES

from .evidence import capture_exception
from .models import CorrelationContext, PromotionReason
from .redaction import redact_text

MAX_EMERGENCY_BYTES = 16_384
MAX_SECONDARY_FAILURES = 4
MAX_SECONDARY_FAILURE_CHARS = 256
T = TypeVar("T")


class FatalDiagnosticService(Protocol):
    def build(
        self,
        *,
        code: str,
        context: CorrelationContext,
        message: str,
        operational_effect: str,
        recovery_action: str,
        promotion_reason: PromotionReason,
        exception: BaseException | None = None,
    ) -> Any: ...

    def promote(self, instance: Any) -> Any: ...


@dataclass(frozen=True)
class FlushResult:
    completed: bool
    failure: str | None = None


@dataclass
class SecondaryFailureLedger:
    _failures: list[str] = field(default_factory=list)

    def retain(self, event: str, error: BaseException) -> None:
        if len(self._failures) < MAX_SECONDARY_FAILURES:
            self._failures.append(_bounded_secondary(event, error))

    def snapshot(self) -> tuple[str, ...]:
        return tuple(self._failures)


def enable_faulthandler() -> bool:
    try:
        if not faulthandler.is_enabled():
            faulthandler.enable(all_threads=True)
        return faulthandler.is_enabled()
    except (OSError, RuntimeError):
        return False


def emergency_bytes(instance: dict[str, Any]) -> bytes:
    evidence = instance.get("exception_evidence") or {}
    lines = [
        f"fatal[{instance['code']}]: {instance['message']}",
        f"role={instance['context']['role']} component={instance['context']['component']}",
        f"instance={instance['context']['instance_id']}",
    ]
    if instance["context"].get("build_identity"):
        lines.append(f"build={instance['context']['build_identity']}")
    if instance["context"].get("configuration_generation") is not None:
        lines.append(f"configuration_generation={instance['context']['configuration_generation']}")
    for failure in instance.get("secondary_failures", ())[:MAX_SECONDARY_FAILURES]:
        lines.append(f"secondary_failure={failure}")
    _render_evidence(evidence, lines, prefix="")
    return _utf8_bytes("\n".join(lines) + "\n", limit=MAX_EMERGENCY_BYTES)


def _utf8_bytes(value: str, *, limit: int) -> bytes:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return encoded
    suffix = "…\n".encode()
    prefix = encoded[: max(0, limit - len(suffix))].decode("utf-8", errors="ignore")
    return prefix.encode() + suffix


def _render_evidence(evidence: Mapping[str, Any], lines: list[str], *, prefix: str) -> None:
    lines.append(f"{prefix}exception={evidence.get('type', 'unknown')}: {evidence.get('message', '')}")
    _render_truncation(evidence, lines, prefix=prefix)
    for frame in _bounded_sequence(evidence.get("frames")):
        if isinstance(frame, Mapping):
            lines.append(
                f"{prefix}  at {frame.get('filename', '')}:{frame.get('line', 0)} in {frame.get('function', '')}"
            )
    for note in _bounded_sequence(evidence.get("notes")):
        lines.append(f"{prefix}  note: {note}")
    if isinstance(evidence.get("cause"), Mapping):
        lines.append(f"{prefix}caused by:")
        _render_evidence(evidence["cause"], lines, prefix=prefix + "  ")
    if isinstance(evidence.get("context"), Mapping):
        lines.append(f"{prefix}during handling:")
        _render_evidence(evidence["context"], lines, prefix=prefix + "  ")
    for index, member in enumerate(_bounded_sequence(evidence.get("members"))):
        if isinstance(member, Mapping):
            lines.append(f"{prefix}group member {index + 1}:")
            _render_evidence(member, lines, prefix=prefix + "  ")


def _render_truncation(evidence: Mapping[str, Any], lines: list[str], *, prefix: str) -> None:
    truncated = evidence.get("truncated")
    if not isinstance(truncated, Mapping):
        return
    for key in sorted(truncated):
        raw_value = truncated[key]
        value = str(raw_value).lower() if isinstance(raw_value, bool | int) else redact_text(raw_value, limit=128)
        lines.append(f"{prefix}truncated[{redact_text(key, limit=64)}]={value}")


def _bounded_sequence(value: object) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    return ()


def direct_stderr(data: bytes) -> None:
    view = memoryview(data[:MAX_EMERGENCY_BYTES])
    while view:
        written = os.write(2, view)
        if written <= 0:
            raise OSError("direct stderr write made no progress")
        view = view[written:]


def flush_logs(*, timeout_seconds: float = 0.2) -> FlushResult:
    failures: list[str] = []

    def flush() -> None:
        try:
            for handler in tuple(logging.getLogger().handlers):
                handler.flush()
        except BaseException as exc:
            failures.append(_bounded_secondary("log_flush_failed", exc))

    thread = threading.Thread(target=flush, name="fatal-log-flush", daemon=True)
    thread.start()
    thread.join(max(0.01, min(timeout_seconds, 1.0)))
    if thread.is_alive():
        logging.raiseExceptions = False
        return FlushResult(False, "log_flush_timeout")
    if failures:
        logging.raiseExceptions = False
        return FlushResult(True, failures[0])
    return FlushResult(True)


def _bounded_secondary(event: str, error: BaseException) -> str:
    detail = redact_text(str(error), limit=MAX_SECONDARY_FAILURE_CHARS)
    return redact_text(f"{event}: {detail}", limit=MAX_SECONDARY_FAILURE_CHARS)


def _secondary_failure(event: str, error: BaseException) -> str:
    bounded = _bounded_secondary(event, error)
    with suppress(BaseException):
        logging.getLogger(__name__).error("%s", bounded)
    return bounded


@dataclass
class FatalBoundary:
    service: FatalDiagnosticService | None
    context: CorrelationContext
    secondary_failures: SecondaryFailureLedger = field(default_factory=SecondaryFailureLedger)

    def report(self, exc: BaseException) -> None:
        secondary = list(self.secondary_failures.snapshot())
        payload: dict[str, Any] = {
            "code": RUNTIME_CODES["fatal_controller"],
            "message": "The process terminated after an uncaught fatal failure.",
            "context": self.context.to_dict(),
            "exception_evidence": capture_exception(exc),
        }
        try:
            if self.service is not None:
                instance = self.service.build(
                    code=RUNTIME_CODES["fatal_controller"],
                    context=self.context,
                    message="The controller terminated after an uncaught fatal failure.",
                    operational_effect="The current process cannot continue safely.",
                    recovery_action="Preserve evidence and exit nonzero so the service manager can record failure.",
                    promotion_reason=PromotionReason.PROCESS_TERMINATION,
                    exception=exc,
                )
                payload = instance.to_dict()
                try:
                    self.service.promote(instance)
                except BaseException as persistence_error:
                    secondary.append(
                        _secondary_failure(
                            "fatal_diagnostic_persistence_failed",
                            persistence_error,
                        )
                    )
        except BaseException as reporting_error:
            secondary.append(_secondary_failure("fatal_diagnostic_build_failed", reporting_error))
        try:
            flush_result = flush_logs()
            if flush_result.failure is not None:
                secondary.append(flush_result.failure)
        except BaseException as flush_error:
            secondary.append(_bounded_secondary("fatal_log_flush_failed", flush_error))
        payload["secondary_failures"] = secondary[:MAX_SECONDARY_FAILURES]
        try:
            direct_stderr(emergency_bytes(payload))
        except BaseException:
            with suppress(BaseException):
                os.write(2, b"fatal[unavailable]: emergency rendering failed\n")

    def run(self, callback: Callable[[], T]) -> T:
        enable_faulthandler()
        try:
            return callback()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            self.report(exc)
            raise

    def run_process(self, callback: Callable[[], object]) -> int:
        """Terminal wrapper that avoids Python's unredacted default renderer."""
        enable_faulthandler()
        try:
            callback()
            return 0
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            self.report(exc)
            return 1

    async def run_async(self, callback: Callable[[], Awaitable[T]]) -> T:
        enable_faulthandler()
        try:
            return await callback()
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as exc:
            self.report(exc)
            raise
