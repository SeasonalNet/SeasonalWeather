"""Bounded controller-owned runtime diagnostic emission ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .models import CorrelationContext, DiagnosticRole, PromotionReason
from .service import RuntimeDiagnosticService


class RuntimeDiagnosticSink:
    """Promote subsystem-owned runtime conditions through the controller service."""

    def __init__(
        self,
        service: RuntimeDiagnosticService,
        context: CorrelationContext,
        *,
        codes: Mapping[str, str],
        generation_provider: Callable[[], int] | None = None,
    ) -> None:
        self._service = service
        self._context = context
        self._codes = dict(codes)
        self._generation_provider = generation_provider

    def emit(
        self,
        code: str,
        *,
        component: str,
        message: str,
        operational_effect: str,
        recovery_action: str,
        promotion_reason: PromotionReason = PromotionReason.DEGRADATION,
        exception: BaseException | None = None,
        source_id: str | None = None,
    ) -> None:
        if code not in self._codes.values():
            raise ValueError("runtime diagnostic code is outside this sink's ownership")
        generation = self._generation_provider() if self._generation_provider is not None else None
        context = CorrelationContext(
            role=DiagnosticRole.CONTROLLER,
            instance_id=self._context.instance_id,
            component=component[:64],
            build_identity=self._context.build_identity,
            configuration_generation=generation,
            source_id=source_id,
            reason_code=code.lower(),
        )
        try:
            instance = self._service.build(
                code=code,
                context=context,
                message=message,
                operational_effect=operational_effect,
                recovery_action=recovery_action,
                promotion_reason=promotion_reason,
                exception=exception,
            )
            self._service.promote(instance)
        except Exception:
            # Diagnostics are best effort and must not become a second
            # subsystem authority or change the original failure outcome.
            return


__all__ = ["RuntimeDiagnosticSink"]
