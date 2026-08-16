"""Controller runtime-diagnostic bridge for the normalized NWWS source."""

from __future__ import annotations

from collections.abc import Callable

from ..diagnostics.bindings import NWWS_CODES
from ..runtime_diagnostics.models import CorrelationContext, DiagnosticRole, PromotionReason
from ..runtime_diagnostics.service import RuntimeDiagnosticService


class NwwsRuntimeDiagnosticSink:
    """Promote adapter failures through the existing runtime-diagnostics owner."""

    def __init__(
        self,
        service: RuntimeDiagnosticService,
        context: CorrelationContext,
        *,
        generation_provider: Callable[[], int] | None = None,
    ) -> None:
        self._service = service
        self._context = context
        self._generation_provider = generation_provider

    def emit(
        self,
        code: str,
        *,
        message: str,
        exception: BaseException | None = None,
    ) -> None:
        if code not in NWWS_CODES.values():
            raise ValueError("unknown NWWS runtime diagnostic code")
        generation = self._generation_provider() if self._generation_provider is not None else None
        instance = self._service.build(
            code=code,
            context=CorrelationContext(
                role=DiagnosticRole.CONTROLLER,
                instance_id=self._context.instance_id,
                component="nwws-source",
                build_identity=self._context.build_identity,
                configuration_generation=generation,
                source_id="nwws-oi",
                reason_code=code.lower(),
            ),
            message=message,
            operational_effect="NWWS-OI source availability or lifecycle is degraded within the controller.",
            recovery_action="Inspect bounded source health and use the existing controller configuration lifecycle.",
            promotion_reason=(
                PromotionReason.OPERATOR_ATTENTION
                if code == NWWS_CODES["auth_failure"]
                else PromotionReason.DEGRADATION
            ),
            exception=exception,
        )
        self._service.promote(instance)


__all__ = ["NwwsRuntimeDiagnosticSink"]
