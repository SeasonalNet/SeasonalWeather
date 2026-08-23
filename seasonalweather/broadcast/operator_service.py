"""Controller-facing broadcast command operations."""

from __future__ import annotations

import contextlib
import datetime as dt
from typing import Any

from seasonalweather.application.errors import ConflictError, DependencyUnavailableError
from seasonalweather.diagnostics.bindings import FOUNDATION_CODES


class BroadcastOperatorService:
    """Own bounded API commands that mutate broadcast runtime state."""

    def __init__(self, orchestrator: Any) -> None:
        self.orch = orchestrator

    def _now_local(self) -> dt.datetime:
        return dt.datetime.now(tz=getattr(self.orch, "_tz", dt.UTC))

    @staticmethod
    def _serialize(value: dt.datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()

    def _ensure_backend_ready(self) -> None:
        try:
            available = bool(self.orch.telnet.ping())
        except Exception as exc:
            self._diagnose(FOUNDATION_CODES["liquidsoap.control_failed"], exc)
            raise DependencyUnavailableError(
                "liquidsoap_unreachable", "Liquidsoap telnet backend is unavailable."
            ) from exc
        if not available:
            self._diagnose(FOUNDATION_CODES["liquidsoap.control_failed"])
            raise DependencyUnavailableError("liquidsoap_unreachable", "Liquidsoap telnet backend is unavailable.")

    def _diagnose(self, code: str, exception: BaseException | None = None) -> None:
        sink = getattr(self.orch, "liquidsoap_diagnostic_sink", None)
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            return
        emit(
            code,
            component="liquidsoap-control",
            message="Liquidsoap control was unavailable for a broadcast operation.",
            operational_effect="The requested broadcast control operation was not applied.",
            recovery_action="Inspect Liquidsoap readiness and retry through the bounded control path.",
            exception=exception,
            source_id="liquidsoap",
        )

    async def rebuild_cycle(self, *, reason: str | None, actor: str) -> dict[str, Any]:
        self._ensure_backend_ready()
        reason_text = (reason or "admin-request").strip() or "admin-request"
        self.orch._schedule_cycle_refill(reason=f"api-{reason_text[:48]}")
        with contextlib.suppress(Exception):
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/cycle/rebuild",
                actor=actor,
                status="succeeded",
                details={"reason": reason_text, "mode": getattr(self.orch, "mode", "unknown")},
            )
        return {"ok": True, "reason": reason_text, "actor": actor, "mode": getattr(self.orch, "mode", "unknown")}

    async def set_heightened_mode(self, *, minutes: int, reason: str, actor: str) -> dict[str, Any]:
        now = self._now_local()
        self.orch.last_heightened_at = now
        self.orch.heightened_until = now + dt.timedelta(minutes=minutes)
        self.orch._update_mode()
        self.orch.last_product_desc = f"Manual heightened mode: {reason}"[:200]
        self.orch._schedule_cycle_refill(reason="api-heightened")
        with contextlib.suppress(Exception):
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/mode/heightened",
                actor=actor,
                status="succeeded",
                details={"minutes": minutes, "reason": reason},
            )
        return {
            "ok": True,
            "mode": getattr(self.orch, "mode", "unknown"),
            "heightened_until": self._serialize(self.orch.heightened_until),
            "reason": reason,
            "actor": actor,
        }

    async def clear_heightened_mode(self, *, reason: str | None, actor: str) -> dict[str, Any]:
        self.orch.heightened_until = None
        self.orch._update_mode()
        self.orch.last_product_desc = (
            f"Manual heightened mode cleared: {reason}" if reason else "Manual heightened mode cleared"
        )[:200]
        self.orch._schedule_cycle_refill(reason="api-clear-heightened")
        with contextlib.suppress(Exception):
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/mode/clear",
                actor=actor,
                status="succeeded",
                details={"reason": reason or ""},
            )
        return {"ok": True, "mode": getattr(self.orch, "mode", "unknown"), "reason": reason, "actor": actor}

    async def originate_test(self, *, event_code: str, actor: str) -> dict[str, Any]:
        self._ensure_backend_ready()
        allowed, why = self.orch.tests_runtime.gate()
        if not allowed:
            raise ConflictError(
                "test_gate_blocked", "Required test origination is currently blocked.", details={"reason": why}
            )
        await self.orch.tests_runtime.originate_required_test(event_code)
        with contextlib.suppress(Exception):
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/originate/test",
                actor=actor,
                status="succeeded",
                details={"event_code": event_code},
            )
        return {"ok": True, "event_code": event_code, "actor": actor}
