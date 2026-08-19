"""Controller-owned application services for segment inspection and refresh."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, cast

from ..commands import CommandStore
from ..commands.contracts import CommandRecord, CommandStatus, CommandType
from ..lifecycle import TaskSupervisor
from .segment_builders import sanitize_error
from .segment_refresher import SegmentRefreshCancelled
from .segment_registry import ResolvedSegmentRegistry
from .segment_store import (
    RefreshEvidenceState,
    RefreshReconciliationOutcome,
    SegmentCommitAmbiguousError,
    SegmentCommitResult,
    SegmentEntry,
    SegmentStore,
    segment_entry_eligible_to_air,
)

log = logging.getLogger("seasonalweather.segment_service")


class SegmentServiceError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class SegmentApplicationService:
    """Read-only projections and one-target command admission.

    The service never touches SQL, TTS, files, Liquidsoap, or conductor
    internals.  Refresh execution is handed to the existing refresher and
    command/lifecycle authorities.
    """

    def __init__(
        self,
        *,
        registry: Callable[[], ResolvedSegmentRegistry],
        store: SegmentStore,
        refresher: Any,
        mode: Callable[[], str],
        supervisor: TaskSupervisor | None = None,
        runtime_snapshot: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._refresher = refresher
        self._mode = mode
        self._supervisor = supervisor
        self._runtime_snapshot = runtime_snapshot
        self._active_refreshes: dict[str, asyncio.Task[Any]] = {}

    def _registry_for_snapshot(self, snapshot: dict[str, Any]) -> ResolvedSegmentRegistry:
        candidate = snapshot.get("_registry")
        return candidate if isinstance(candidate, ResolvedSegmentRegistry) else self._registry()

    @staticmethod
    def _resolved_runtime_item(registry: ResolvedSegmentRegistry, key: str) -> Any:
        return next((item for item in registry.definitions if item.definition.key == key), None)

    @staticmethod
    def _iso(value: str | None) -> str | None:
        return value[:64] if value else None

    def _entry_projection(self, registry: ResolvedSegmentRegistry, resolved: Any) -> dict[str, Any]:
        definition = resolved.definition
        entry: SegmentEntry | None = self._store.get(definition.key)
        provenance = entry.provenance if entry is not None else None
        stale = bool(entry is None or entry.is_expired())
        placeholder = bool(entry is None or entry.is_placeholder)
        return {
            "key": definition.key,
            "title": definition.title,
            "enabled": bool(resolved.enabled),
            "build_role": definition.builder.kind.value,
            "builder": definition.builder.operation,
            "refreshable": bool(registry.refreshable(definition.key)),
            "refresh_cadence_seconds": definition.refresh_cadence_seconds,
            "maximum_age_seconds": definition.max_age_seconds,
            "minimum_air_interval_seconds": definition.minimum_air_interval_seconds,
            "freshness": "placeholder" if placeholder else ("stale" if stale else "fresh"),
            "stale": stale,
            "placeholder": placeholder,
            "ready": bool(entry is not None and self._store.is_ready(definition.key)),
            "failure": self._failure_projection(provenance),
            "provenance": self._provenance_projection(provenance),
        }

    def _failure_projection(self, provenance: Any) -> dict[str, Any]:
        if provenance is None:
            return {"last_error": None, "consecutive_failures": 0}
        return {
            "last_error": self._iso(provenance.last_error),
            "consecutive_failures": int(provenance.consecutive_failures),
        }

    def _provenance_projection(self, provenance: Any) -> dict[str, Any]:
        fields = (
            "source_name",
            "product_identifier",
            "product_type",
            "issuing_office",
            "issuance_time",
            "fetch_time",
            "last_successful_synthesis",
            "source_reference",
            "last_aired",
            "next_eligible_airtime",
        )
        if provenance is None:
            return {field: None for field in fields} | {"current_content_hash": None}
        values = asdict(provenance)
        return {field: self._iso(values.get(field)) for field in fields} | {
            "current_content_hash": provenance.current_content_hash
        }

    def list_segments(self) -> dict[str, Any]:
        registry = self._registry()
        return {"segments": [self._entry_projection(registry, item) for item in registry.definitions]}

    def get_segment(self, key: str) -> dict[str, Any]:
        normalized = str(key or "").strip()
        if len(normalized) > 64 or not normalized:
            raise SegmentServiceError("invalid_segment_key", "Segment key is invalid.")
        registry = self._registry()
        definition = registry.get(normalized)
        if definition is None:
            raise SegmentServiceError("segment_not_found", "Segment was not found.", status_code=404)
        resolved = next(item for item in registry.definitions if item.definition.key == definition.key)
        return self._entry_projection(registry, resolved)

    def cycle_plan(self) -> dict[str, Any]:
        snapshot = self._runtime_snapshot() if self._runtime_snapshot is not None else {}
        registry = self._registry_for_snapshot(snapshot)
        mode = snapshot.get("mode")
        if mode is None and self._runtime_snapshot is None:
            mode = self._mode()
        return {
            "mode": mode or "",
            "normal": [{"key": key, "title": registry.title_for(key)} for key in registry.static_order(focus=False)],
            "focus": [{"key": key, "title": registry.title_for(key)} for key in registry.static_order(focus=True)],
            "dynamic": {"alerts": True, "scheduled_inserts": True},
        }

    def cycle_preview(self) -> dict[str, Any]:
        snapshot = self._runtime_snapshot() if self._runtime_snapshot is not None else {}
        registry = self._registry_for_snapshot(snapshot)
        mode = snapshot.get("mode")
        if mode is None and self._runtime_snapshot is None:
            mode = self._mode()
        focus = bool(snapshot.get("focus", str(mode or "").lower() == "heightened"))
        deferred_due_order = tuple(str(key) for key in snapshot.get("deferred_due_keys", ()))
        deferred_due_keys = set(deferred_due_order)
        deferred_keys = {str(key) for key in snapshot.get("deferred_keys", ())}
        runtime_items = tuple(snapshot.get("runtime_items", ()))
        if not runtime_items:
            keys = list(registry.static_order(focus=focus))
            if focus:
                keys.extend(key for key in deferred_due_order if key not in keys)
            runtime_items = tuple({"key": key, "kind": "static"} for key in keys)
        selected = []
        for runtime_item in runtime_items:
            key = str(runtime_item.get("key") or "")
            if runtime_item.get("kind") == "static":
                resolved = self._resolved_runtime_item(registry, key)
                if resolved is None:
                    # A runtime projection from another generation cannot
                    # authorize a segment absent from this response registry.
                    continue
                selected.append(
                    self._preview_entry(
                        registry,
                        resolved,
                        focus=focus,
                        deferred_keys=deferred_keys,
                        deferred_due_keys=deferred_due_keys,
                    )
                )
            else:
                if runtime_item.get("kind") == "alert":
                    ready = segment_entry_eligible_to_air(self._store.get(key))
                else:
                    ready = bool(runtime_item.get("ready"))
                selected.append(
                    {
                        "key": key,
                        "title": str(runtime_item.get("title") or key),
                        "kind": str(runtime_item.get("kind") or "dynamic"),
                        "selected": ready,
                        "eligible_to_air": ready,
                        "deferred": False,
                        "deferred_due": False,
                        "freshness": "fresh" if ready else "placeholder",
                        "placeholder": not ready,
                        "last_aired": None,
                        "next_eligible_airtime": None,
                    }
                )
        return {
            "mode": mode,
            "focus": focus,
            "deferred": tuple(snapshot.get("deferred", ())),
            "order": tuple(item["key"] for item in selected),
            "segments": selected,
            "read_only": True,
        }

    def _preview_entry(
        self,
        registry: ResolvedSegmentRegistry,
        item: Any,
        *,
        focus: bool,
        deferred_keys: set[str],
        deferred_due_keys: set[str],
    ) -> dict[str, Any]:
        projection = self._entry_projection(registry, item)
        next_eligible = projection["provenance"].get("next_eligible_airtime")
        eligible = bool(item.enabled and segment_entry_eligible_to_air(self._store.get(item.definition.key)))
        deferred = focus and (item.definition.key in deferred_keys or item.definition.focus_policy.value == "deferred")
        deferred_due = item.definition.key in deferred_due_keys
        return {
            "key": item.definition.key,
            "title": item.definition.title,
            "kind": "static",
            "selected": bool(eligible and (not deferred or deferred_due)),
            "eligible_to_air": eligible,
            "deferred": deferred,
            "deferred_due": deferred_due,
            "freshness": projection["freshness"],
            "placeholder": projection["placeholder"],
            "last_aired": projection["provenance"].get("last_aired"),
            "next_eligible_airtime": next_eligible,
        }

    async def accept_refresh(
        self,
        *,
        key: str,
        actor: str,
        idempotency_key: str,
        command_store: CommandStore,
        request_id: str | None = None,
    ) -> tuple[Any, bool]:
        normalized = str(key or "").strip()
        registry = self._registry()
        definition = registry.get(normalized)
        if definition is None:
            raise SegmentServiceError("segment_not_found", "Segment was not found.", status_code=404)
        if not registry.enabled(definition.key):
            raise SegmentServiceError("segment_disabled", "The requested segment is disabled.", status_code=409)
        if not registry.refreshable(definition.key):
            raise SegmentServiceError(
                "segment_not_refreshable", "The requested segment is not independently refreshable.", status_code=409
            )
        if self._supervisor is None or not self._supervisor.lifecycle.permits_service_start():
            raise SegmentServiceError(
                "segment_refresh_unavailable",
                "Segment refresh supervision is unavailable.",
                status_code=503,
            )
        payload = {"segment_key": definition.key}
        record, replayed = await command_store.create_or_replay(
            command_type=CommandType.SEGMENT_REFRESH.value,
            idempotency_key=idempotency_key,
            actor=actor,
            payload=payload,
            reason=f"segment-refresh:{definition.key}",
            request_id=request_id,
        )
        if replayed:
            return await self._replay_refresh(command_store, record)
        await self._schedule_refresh(command_store, record.command_id, definition.key)
        return record, False

    async def _replay_refresh(self, command_store: CommandStore, record: CommandRecord) -> tuple[CommandRecord, bool]:
        if record.status in {CommandStatus.ACCEPTED, CommandStatus.RUNNING} and not self._refresh_is_live(
            record.command_id
        ):
            await self._reconcile_orphaned_refresh(command_store, record)
            record = await command_store.get(record.command_id)
        return record, True

    async def _schedule_refresh(self, command_store: CommandStore, command_id: str, key: str) -> None:
        coroutine = self._run_refresh(command_store, command_id, key)
        supervisor = cast(TaskSupervisor, self._supervisor)
        try:
            task = supervisor.create_task(
                coroutine,
                name=f"segment-refresh-{command_id}",
                required=False,
            )
        except Exception:
            try:
                await command_store.mark_cancelled(command_id)
            except Exception:
                log.exception(
                    "segment refresh admission cleanup failed command_id=%s key=%s",
                    command_id,
                    key,
                )
            raise
        self._active_refreshes[command_id] = task
        task.add_done_callback(lambda _completed: self._active_refreshes.pop(command_id, None))

    def _refresh_is_live(self, command_id: str) -> bool:
        task = self._active_refreshes.get(command_id)
        return task is not None and not task.done()

    @staticmethod
    def _segment_key_for_command(record: CommandRecord) -> str | None:
        prefix = "segment-refresh:"
        if record.reason is None or not record.reason.startswith(prefix):
            return None
        key = record.reason.removeprefix(prefix)
        return key if 0 < len(key) <= 64 else None

    async def reconcile_orphaned_refreshes(self, command_store: CommandStore, *, limit: int = 256) -> int:
        """Terminalize lost refresh executions after publication recovery."""
        reconciled = 0
        async for records in command_store.iter_nonterminal_pages(
            CommandType.SEGMENT_REFRESH,
            page_size=limit,
        ):
            for record in records:
                if self._refresh_is_live(record.command_id):
                    continue
                if await self._reconcile_orphaned_refresh(command_store, record):
                    reconciled += 1
        return reconciled

    async def _reconcile_orphaned_refresh(self, command_store: CommandStore, record: CommandRecord) -> bool:
        key = self._segment_key_for_command(record)
        if key is None:
            return False
        try:
            outcome = await self._store.reconcile_committed_refresh_command(command_store, record.command_id, key)
            if outcome is RefreshReconciliationOutcome.PUBLICATION_PROVEN:
                return True
        except Exception:
            log.exception(
                "segment refresh orphan reconciliation deferred command_id=%s key=%s",
                record.command_id,
                key,
            )
            return False
        if outcome is RefreshReconciliationOutcome.STILL_UNRESOLVED:
            log.warning(
                "segment refresh orphan reconciliation deferred for unresolved evidence command_id=%s key=%s",
                record.command_id,
                key,
            )
            return False
        try:
            await command_store.mark_cancelled(record.command_id)
        except Exception:
            log.exception(
                "segment refresh orphan terminalization failed command_id=%s key=%s",
                record.command_id,
                key,
            )
            return False
        return True

    async def _repair_post_commit_terminalization(
        self, command_store: CommandStore, command_id: str, key: str
    ) -> RefreshReconciliationOutcome:
        try:
            outcome = await self._store.reconcile_committed_refresh_command(command_store, command_id, key)
            if outcome is not RefreshReconciliationOutcome.PUBLICATION_PROVEN:
                log.warning(
                    "segment refresh post-commit repair remains %s command_id=%s key=%s",
                    outcome.value,
                    command_id,
                    key,
                )
            return outcome
        except Exception:
            log.exception(
                "segment refresh post-commit repair failed command_id=%s key=%s",
                command_id,
                key,
            )
            return RefreshReconciliationOutcome.STILL_UNRESOLVED

    async def _terminalize_unproven_refresh(
        self, command_store: CommandStore, command_id: str, outcome: RefreshReconciliationOutcome
    ) -> None:
        if outcome is RefreshReconciliationOutcome.PUBLICATION_NOT_PROVEN:
            await command_store.mark_cancelled(command_id)

    async def _run_refresh(self, command_store: CommandStore, command_id: str, key: str) -> None:
        publication_won = False
        publication_lock_depth = 0

        def publication_fence() -> None:
            nonlocal publication_lock_depth
            begin = getattr(command_store, "begin_publication", None)
            if callable(begin):
                if not begin(command_id):
                    raise SegmentRefreshCancelled
                publication_lock_depth += 1
            elif not publication_won and command_store.cancellation_requested(command_id):
                raise SegmentRefreshCancelled

        def publication_committed(result: SegmentCommitResult | None = None) -> None:
            nonlocal publication_won, publication_lock_depth
            if result is not None and not result.committed:
                return
            publication_won = True
            if publication_lock_depth:
                released = command_store.finish_publication(command_id)
                if released is not False:
                    publication_lock_depth -= 1

        def publication_aborted() -> None:
            nonlocal publication_lock_depth
            if publication_lock_depth:
                # The async bridge invokes this callback on the worker that
                # acquired the gate. A later service-side observation sees
                # depth zero and is a no-op; it never probes lock ownership by
                # attempting a wrong-thread release.
                released = command_store.finish_publication(command_id)
                if released is not False:
                    publication_lock_depth -= 1

        try:
            await command_store.mark_running(command_id)
            if command_store.cancellation_requested(command_id):
                await command_store.mark_cancelled(command_id)
                return
            refresh_result = await self._refresher.refresh_one(
                key,
                commit_guard=publication_fence,
                commit_won=publication_committed,
                commit_aborted=publication_aborted,
                commit_identity=command_id,
            )
            self._ensure_refresh_committed(publication_won, refresh_result)
            # The segment publication is the authority boundary.  Once it has
            # won, command terminalization is post-commit bookkeeping: a
            # persistence or broker failure must leave the exact command
            # correlation recoverable, never rewrite the committed refresh as
            # FAILED.
            try:
                await command_store.mark_succeeded(
                    command_id,
                    {"code": "segment_refresh_completed", "segment_key": key},
                )
            except Exception:
                log.exception(
                    "segment refresh committed but success terminalization failed command_id=%s key=%s",
                    command_id,
                    key,
                )
                await self._repair_post_commit_terminalization(command_store, command_id, key)
                return
            try:
                self._store.acknowledge_refresh_command(command_id, key)
            except Exception:
                log.exception(
                    "segment refresh succeeded but receipt acknowledgement failed command_id=%s key=%s",
                    command_id,
                    key,
                )
        except SegmentRefreshCancelled:
            publication_aborted()
            await command_store.mark_cancelled(command_id)
        except asyncio.CancelledError:
            publication_aborted()
            await self._handle_refresh_cancellation(command_store, command_id, key, publication_won)
            raise
        except SegmentCommitAmbiguousError:
            publication_aborted()
            outcome = await self._repair_post_commit_terminalization(command_store, command_id, key)
            await self._terminalize_unproven_refresh(command_store, command_id, outcome)
        except SegmentServiceError as exc:
            publication_aborted()
            await command_store.mark_failed(command_id, {"code": exc.code, "message": exc.message})
        except Exception as exc:
            publication_aborted()
            await command_store.mark_failed(
                command_id,
                {
                    "code": "segment_refresh_failed",
                    "message": "Segment refresh failed.",
                    "details": {"reason": sanitize_error(exc)},
                },
            )

    @staticmethod
    def _ensure_refresh_committed(publication_won: bool, refresh_result: Any) -> None:
        if publication_won or (isinstance(refresh_result, SegmentCommitResult) and refresh_result.committed):
            return
        raise SegmentServiceError("segment_refresh_failed", "Segment refresh did not produce an accepted candidate.")

    async def _handle_refresh_cancellation(
        self,
        command_store: CommandStore,
        command_id: str,
        key: str,
        publication_won: bool,
    ) -> None:
        evidence = self._store.refresh_evidence_state(key, command_id)
        if publication_won or evidence is not RefreshEvidenceState.NONE:
            await asyncio.shield(self._repair_post_commit_terminalization(command_store, command_id, key))
        else:
            await asyncio.shield(command_store.mark_cancelled(command_id))


__all__ = ["SegmentApplicationService", "SegmentServiceError"]
