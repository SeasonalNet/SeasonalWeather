"""Promotion, trusted catalog policy, fingerprint, and lifecycle coordination."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from typing import Any

from seasonalweather.diagnostics import load_catalog
from seasonalweather.diagnostics.models import DiagnosticDefinition

from .evidence import capture_exception
from .fingerprint import fingerprint
from .models import (
    OCCURRENCE_SCHEMA_VERSION,
    RUNTIME_DIAGNOSTIC_SCHEMA_VERSION,
    CorrelationContext,
    PromotionReason,
    ResolutionEvidence,
    RuntimeDiagnostic,
    TransitionIntent,
)
from .redaction import redact_text
from .repository import OccurrenceRepository, RecordResult


class RuntimeDiagnosticService:
    def __init__(
        self,
        repository: OccurrenceRepository,
        *,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))

    def initialize(self) -> None:
        self.repository.initialize()

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
        exception_evidence: dict[str, Any] | None = None,
        transition_intent: TransitionIntent = TransitionIntent.ACTIVATE,
    ) -> RuntimeDiagnostic:
        catalog = load_catalog()
        definition = catalog.definition(code)
        if definition is None:
            raise ValueError("runtime diagnostic code is not defined by the local catalog")
        return _instance(
            definition,
            context=context,
            message=message,
            operational_effect=operational_effect,
            recovery_action=recovery_action,
            promotion_reason=promotion_reason,
            observed_at=self.clock(),
            catalog_version=catalog.diagnostic_catalog_version,
            exception=exception,
            exception_evidence=exception_evidence,
            transition_intent=transition_intent,
        )

    def promote(self, instance: RuntimeDiagnostic) -> RecordResult:
        if instance.transition_intent is not TransitionIntent.ACTIVATE:
            raise ValueError("resolution requires an existing controller occurrence identity")
        return self.repository.record(instance, fingerprint(instance))

    def resolve(
        self,
        occurrence_id: str,
        *,
        reason: str,
        evidence: ResolutionEvidence | Mapping[str, object] | None = None,
    ):
        bounded_evidence = (
            evidence if isinstance(evidence, ResolutionEvidence) else ResolutionEvidence.from_mapping(evidence)
        )
        return self.repository.resolve(
            occurrence_id,
            observed_at=self.clock(),
            reason=redact_text(reason, limit=512),
            evidence=bounded_evidence,
        )

    def prune_resolved(
        self,
        *,
        retention_days: int = 90,
        retain_resolved: int = 1_000,
    ) -> int:
        bounded_days = max(1, min(retention_days, 3_650))
        bounded_retain = max(0, min(retain_resolved, 100_000))
        return self.repository.prune(
            resolved_before=self.clock() - dt.timedelta(days=bounded_days),
            retain_resolved=bounded_retain,
        )


def _instance(
    definition: DiagnosticDefinition,
    *,
    context: CorrelationContext,
    message: str,
    operational_effect: str,
    recovery_action: str,
    promotion_reason: PromotionReason,
    observed_at: dt.datetime,
    catalog_version: int,
    exception: BaseException | None,
    exception_evidence: dict[str, Any] | None,
    transition_intent: TransitionIntent,
) -> RuntimeDiagnostic:
    return RuntimeDiagnostic(
        code=str(definition.code),
        diagnostic_schema_version=RUNTIME_DIAGNOSTIC_SCHEMA_VERSION,
        catalog_version=catalog_version,
        occurrence_schema_version=OCCURRENCE_SCHEMA_VERSION,
        severity=definition.default_severity,
        blocking=definition.default_blocking,
        fatal=definition.default_fatal,
        retryable=definition.default_retryable,
        context=context,
        message=redact_text(message, limit=512),
        operational_effect=redact_text(operational_effect, limit=512),
        recovery_action=redact_text(recovery_action, limit=512),
        promotion_reason=promotion_reason,
        transition_intent=transition_intent,
        observed_at=observed_at,
        exception_evidence=(capture_exception(exception) if exception is not None else exception_evidence),
    )
