from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from ..diagnostics.bindings import FOUNDATION_CODES
from ..job_admission import JobAdmissionService
from ..jobs.policies import JobType
from ..lifecycle import Lifecycle
from .models import DurableAdmission, ReconciliationSummary, RepositoryHealth
from .repository import JobRepository


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


class DurableJobService:
    """Application boundary composing P1-06 admission with durable commit."""

    def __init__(
        self,
        repository: JobRepository,
        lifecycle: Lifecycle,
        *,
        reconciliation_batch_size: int,
        clock: Callable[[], dt.datetime] = _utc_now,
        diagnostic_sink: object | None = None,
    ) -> None:
        self.repository = repository
        self.lifecycle = lifecycle
        self.clock = clock
        self.reconciliation_batch_size = int(reconciliation_batch_size)
        self._admission = JobAdmissionService(lifecycle, clock=clock)
        self._reconciled = False
        self._diagnostic_sink = diagnostic_sink

    def initialize(self) -> ReconciliationSummary:
        self.repository.initialize()
        summary = self.reconcile()
        self._reconciled = True
        if summary.released_unacknowledged or summary.uncertain_attempts or summary.command_sync_pending:
            self._diagnose(
                FOUNDATION_CODES["job.reconciliation_required"],
                "Job repository startup reconciliation found unfinished or uncertain work.",
            )
        return summary

    def admit(
        self,
        *,
        job_type: JobType,
        payload: dict[str, Any],
        command_id: str | None = None,
        deadline_at: dt.datetime | None = None,
        not_before: dt.datetime | None = None,
        dedupe_key: str | None = None,
        config_generation: int | None = None,
    ) -> DurableAdmission:
        if not self._reconciled:
            raise RuntimeError("job repository must reconcile before admission")
        try:
            specification = self._admission.admit(
                job_type=job_type,
                payload=payload,
                command_id=command_id,
                deadline_at=deadline_at,
                not_before=not_before,
                dedupe_key=dedupe_key,
                config_generation=config_generation,
            )
        except (TypeError, ValueError) as exc:
            self._diagnose(
                FOUNDATION_CODES["job.contract_incompatible"],
                "A job request did not satisfy the admitted job contract.",
                exc,
            )
            raise
        return self.repository.admit(specification, at=self.clock())

    def reconcile(self) -> ReconciliationSummary:
        return self.repository.reconcile(
            now=self.clock(),
            batch_size=self.reconciliation_batch_size,
        )

    def health(self) -> RepositoryHealth:
        return self.repository.health(
            now=self.clock(),
            admission_open=self.lifecycle.ready and self._reconciled,
        )

    def close(self) -> ReconciliationSummary:
        summary = self.repository.reconcile_for_shutdown(
            now=self.clock(),
            batch_size=self.reconciliation_batch_size,
        )
        self.repository.database.close()
        return summary

    def _diagnose(self, code: str, message: str, exception: BaseException | None = None) -> None:
        sink = self._diagnostic_sink
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            return
        emit(
            code,
            component="job-repository",
            message=message,
            operational_effect="Durable job admission or reconciliation requires operator attention.",
            recovery_action="Inspect the bounded job repository state before admitting dependent work.",
            exception=exception,
            source_id="job-store",
        )
