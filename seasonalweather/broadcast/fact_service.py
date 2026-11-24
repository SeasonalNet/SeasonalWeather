"""Controller entry point for optional archive-backed fact computation."""

from datetime import UTC, datetime

from ..database.fact_storage import (
    ArchiveRef,
    CandidateConflictError,
    CandidateProvenanceError,
    CandidateRow,
    PostgresFactCandidateRepository,
)
from ..diagnostics.bindings import FOUNDATION_CODES
from ..runtime_diagnostics.models import PromotionReason
from ..runtime_diagnostics.sink import RuntimeDiagnosticSink
from .fact_engine import DERIVATION_VERSION, FactInvariantError, compare_fact
from .fact_models import BroadcastFact, FactKind, FactResult, FactSample, FactStatus, aware
from .fact_sources import normalize_source, source_table
from .fact_speech import canonical_value, render_fact


def _sample_payload(sample: FactSample) -> dict[str, object]:
    return {
        "value": canonical_value(sample.value),
        "areas": list(sample.areas),
        "measured_at": sample.measured_at.astimezone(UTC).isoformat(),
        "valid_until": sample.valid_until.astimezone(UTC).isoformat(),
        "forecast_period": [time.astimezone(UTC).isoformat() for time in sample.forecast_period]
        if sample.forecast_period
        else None,
    }


def _candidate(fact: BroadcastFact, current: ArchiveRef, previous: ArchiveRef) -> CandidateRow:
    return CandidateRow(
        fact.id,
        fact.expires_at,
        {
            "version": DERIVATION_VERSION,
            "kind": fact.kind.value,
            "identity": fact.current.identity,
            "current": _sample_payload(fact.current),
            "previous": _sample_payload(fact.previous),
            "areas": list(fact.areas),
            "valid_from": fact.valid_from.isoformat(),
            "priority": fact.priority,
            "confidence": fact.confidence.value,
            "speech": render_fact(fact),
        },
        current,
        previous,
        valid_from=fact.valid_from,
    )


class BroadcastFactService:
    """Compute on demand; no polling, selection, or publication authority."""

    def __init__(
        self, repository: PostgresFactCandidateRepository, *, diagnostics: RuntimeDiagnosticSink | None = None
    ) -> None:
        self._repository = repository
        self._diagnostics = diagnostics

    def _diagnose(self, *, invariant: bool) -> None:
        if self._diagnostics is None:
            return
        rule = "facts.invariant_violation" if invariant else "facts.insufficient_history"
        self._diagnostics.emit(
            FOUNDATION_CODES[rule],
            component="broadcast-facts",
            message="Derived fact evidence violated an invariant."
            if invariant
            else "Comparable fact history is insufficient.",
            operational_effect="The requested optional fact is withheld; basic broadcasting is unchanged.",
            recovery_action="Inspect the archive provenance and computation policy."
            if invariant
            else "Check that the requested input has compatible recent archive history.",
            promotion_reason=PromotionReason.INVARIANT_VIOLATION if invariant else PromotionReason.DEGRADATION,
        )

    def _sample(self, kind: FactKind, ref: ArchiveRef, identity: str) -> FactSample | None:
        if ref.table is not source_table(kind):
            raise FactInvariantError("Source table does not match fact kind")
        record = self._repository.lookup(ref)
        if record is None:
            raise FactInvariantError("Explicit archive reference is missing")
        if record.id != ref.id or record.table is not ref.table:
            raise FactInvariantError("Archive returned a different source record")
        return normalize_source(kind, record, identity)

    def compute(
        self, kind: FactKind, identity: str, current: ArchiveRef, previous: ArchiveRef | None, *, as_of: datetime
    ) -> FactResult:
        """Recompute one bounded comparison and persist the result if admissible."""
        if not aware(as_of) or not 0 < len(identity) <= 128 or not identity.isprintable():
            raise ValueError("Fact request needs a bounded identity and timezone-aware time")
        try:
            new = self._sample(kind, current, identity)
            if new is None:
                return FactResult(FactStatus.UNSUPPORTED)
            old = self._sample(kind, previous, identity) if previous is not None else None
            result = compare_fact(kind, new, old, as_of=as_of)
            if result.fact is not None and previous is not None:
                self._repository.store(_candidate(result.fact, current, previous))
        except (FactInvariantError, CandidateConflictError, CandidateProvenanceError):
            self._diagnose(invariant=True)
            raise
        if result.status is FactStatus.INSUFFICIENT_HISTORY:
            self._diagnose(invariant=False)
        return result
