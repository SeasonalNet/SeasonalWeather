from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import uuid
from contextlib import suppress
from typing import Any

from ..database.commands import CommandRepository
from ..database.core import SeasonalDatabase
from ..lifecycle import Lifecycle, WorkClass
from .contracts import (
    CommandAuditContext,
    CommandError,
    CommandRecord,
    CommandRelationshipPolicy,
    CommandResult,
    CommandStatus,
    CommandType,
    RelationshipCompletion,
    command_error_from_mapping,
    command_result_from_mapping,
    request_command_cancellation,
    transition_command,
)


class IdempotencyConflictError(Exception):
    pass


class CommandNotFoundError(KeyError):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def _restore_result(raw: Any) -> CommandResult | None:
    if not isinstance(raw, dict):
        return None
    if "code" in raw and "message" in raw:
        return CommandResult.model_validate(raw)
    return command_result_from_mapping(raw)


def _restore_error(raw: Any) -> CommandError | None:
    if not isinstance(raw, dict):
        return None
    if "code" in raw and "message" in raw:
        return CommandError.model_validate(raw)
    return command_error_from_mapping(raw)


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        item = {
            "event": event_type,
            "data": payload,
            "emitted_at": utc_now().isoformat(),
        }
        async with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(item)


class CommandStore:
    """Application service adapting typed commands to the existing command repository."""

    def __init__(
        self,
        broker: EventBroker | None = None,
        database: SeasonalDatabase | None = None,
        lifecycle: Lifecycle | None = None,
        *,
        clock: Any = utc_now,
    ) -> None:
        self._broker = broker or EventBroker()
        self._lock = asyncio.Lock()
        self._commands_by_id: dict[str, CommandRecord] = {}
        self._commands_by_idempotency_key: dict[str, str] = {}
        self._repo = CommandRepository(database) if database is not None else None
        self._lifecycle = lifecycle
        self._clock = clock

    @property
    def broker(self) -> EventBroker:
        return self._broker

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _relationship_for(command_type: CommandType) -> CommandRelationshipPolicy:
        if command_type in {CommandType.CYCLE_REBUILD, CommandType.CONFIG_RELOAD}:
            return CommandRelationshipPolicy(
                completion=RelationshipCompletion.CONTROLLER_FINALIZATION,
                controller_finalization_required=True,
            )
        return CommandRelationshipPolicy(completion=RelationshipCompletion.NO_JOBS)

    @staticmethod
    def _parse_timestamp(value: Any, *, fallback: dt.datetime | None = None) -> dt.datetime:
        if isinstance(value, dt.datetime):
            parsed = value
        elif value:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        elif fallback is not None:
            parsed = fallback
        else:
            raise ValueError("command timestamp is required")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(dt.UTC)

    @classmethod
    def _record_from_dict(cls, raw: dict[str, Any]) -> CommandRecord:
        accepted_at = cls._parse_timestamp(raw["accepted_at"])
        status_value = "accepted" if str(raw["status"]) == "pending" else str(raw["status"])
        command_type = CommandType(str(raw["command_type"]))
        return CommandRecord(
            command_id=str(raw["command_id"]),
            command_type=command_type,
            status=CommandStatus(status_value),
            actor=str(raw["actor"]),
            reason=raw.get("reason"),
            idempotency_key=str(raw["idempotency_key"]),
            request_id=str(raw.get("request_id") or f"req_{uuid.uuid4().hex[:16]}"),
            correlation_id=raw.get("correlation_id"),
            payload_hash=str(raw["payload_hash"]),
            created_at=cls._parse_timestamp(raw.get("created_at"), fallback=accepted_at),
            accepted_at=accepted_at,
            started_at=cls._parse_timestamp(raw["started_at"]) if raw.get("started_at") else None,
            finished_at=cls._parse_timestamp(raw["finished_at"]) if raw.get("finished_at") else None,
            cancel_requested_at=(
                cls._parse_timestamp(raw["cancel_requested_at"]) if raw.get("cancel_requested_at") else None
            ),
            idempotent_replay_count=int(raw.get("idempotent_replay_count", 0) or 0),
            result=_restore_result(raw.get("result")),
            error=_restore_error(raw.get("error")),
            audit_context=CommandAuditContext.model_validate(
                raw.get("audit_context") or {"channel": "api", "attributes": {}}
            ),
            relationship=cls._relationship_for(command_type),
        )

    @staticmethod
    def _record_to_dict(record: CommandRecord) -> dict[str, Any]:
        raw = record.model_dump(mode="json")
        raw["status"] = record.status.value
        raw["command_type"] = record.command_type.value
        raw["result"] = record.result.model_dump(mode="json") if record.result is not None else None
        raw["error"] = record.error.model_dump(mode="json") if record.error is not None else None
        raw["audit_context"] = record.audit_context.model_dump(mode="json")
        return raw

    def _persist_record(self, record: CommandRecord, *, insert: bool = False) -> None:
        if self._repo is None:
            return
        raw = self._record_to_dict(record)
        if insert:
            self._repo.insert(raw)
        else:
            self._repo.update(raw)

    async def _find(self, command_id: str) -> CommandRecord:
        record = self._commands_by_id.get(command_id)
        if record is None and self._repo is not None:
            raw = self._repo.get_by_command_id(command_id)
            if raw is not None:
                record = self._record_from_dict(raw)
                self._commands_by_id[command_id] = record
                self._commands_by_idempotency_key[record.idempotency_key] = record.command_id
        if record is None:
            raise CommandNotFoundError(command_id)
        return record

    async def create_or_replay(
        self,
        *,
        command_type: str,
        idempotency_key: str,
        actor: str,
        payload: dict[str, Any],
        reason: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[CommandRecord, bool]:
        if self._lifecycle is not None:
            self._lifecycle.require(WorkClass.COMMAND)
        typed_command = CommandType(command_type)
        payload_hash = self._payload_hash(payload)
        async with self._lock:
            existing_id = self._commands_by_idempotency_key.get(idempotency_key)
            existing: CommandRecord | None = None
            if existing_id is not None:
                existing = self._commands_by_id[existing_id]
            elif self._repo is not None:
                raw = self._repo.get_by_idempotency_key(idempotency_key)
                if raw is not None:
                    existing = self._record_from_dict(raw)
                    self._commands_by_id[existing.command_id] = existing
                    self._commands_by_idempotency_key[idempotency_key] = existing.command_id

            if existing is not None:
                if existing.payload_hash != payload_hash or existing.command_type is not typed_command:
                    raise IdempotencyConflictError("idempotency key was reused with a different request")
                existing = CommandRecord.model_validate(
                    existing.model_dump() | {"idempotent_replay_count": existing.idempotent_replay_count + 1}
                )
                self._commands_by_id[existing.command_id] = existing
                self._persist_record(existing)
                await self._broker.publish(
                    "command.replayed",
                    {
                        "command_id": existing.command_id,
                        "command_type": existing.command_type.value,
                        "request_id": existing.request_id,
                    },
                )
                return existing, True

            now = self._clock()
            record = CommandRecord(
                command_id=f"cmd_{uuid.uuid4().hex[:20]}",
                command_type=typed_command,
                actor=actor,
                reason=reason,
                idempotency_key=idempotency_key,
                request_id=request_id or f"req_{uuid.uuid4().hex[:16]}",
                correlation_id=correlation_id,
                payload_hash=payload_hash,
                created_at=now,
                accepted_at=now,
                audit_context=CommandAuditContext(channel="api"),
                relationship=self._relationship_for(typed_command),
            )
            self._commands_by_id[record.command_id] = record
            self._commands_by_idempotency_key[idempotency_key] = record.command_id
            self._persist_record(record, insert=True)

        await self._broker.publish(
            "command.accepted",
            {
                "command_id": record.command_id,
                "command_type": record.command_type.value,
                "request_id": record.request_id,
                "actor": record.actor,
            },
        )
        return record, False

    async def _transition(
        self,
        command_id: str,
        target: CommandStatus,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> CommandRecord:
        async with self._lock:
            record = await self._find(command_id)
            record = transition_command(
                record,
                target,
                at=self._clock(),
                result=command_result_from_mapping(result or {}) if target is CommandStatus.SUCCEEDED else None,
                error=command_error_from_mapping(error or {}) if target is CommandStatus.FAILED else None,
            )
            self._commands_by_id[command_id] = record
            self._persist_record(record)
        await self._broker.publish(
            f"command.{target.value}",
            {
                "command_id": record.command_id,
                "command_type": record.command_type.value,
                "request_id": record.request_id,
            },
        )
        return record

    async def mark_running(self, command_id: str) -> CommandRecord:
        return await self._transition(command_id, CommandStatus.RUNNING)

    async def mark_succeeded(self, command_id: str, result: dict[str, Any]) -> CommandRecord:
        return await self._transition(command_id, CommandStatus.SUCCEEDED, result=result)

    async def mark_failed(self, command_id: str, error: dict[str, Any]) -> CommandRecord:
        return await self._transition(command_id, CommandStatus.FAILED, error=error)

    async def request_cancellation(self, command_id: str) -> CommandRecord:
        async with self._lock:
            record = request_command_cancellation(await self._find(command_id), at=self._clock())
            self._commands_by_id[command_id] = record
            self._persist_record(record)
        await self._broker.publish(
            "command.cancellation_requested",
            {"command_id": record.command_id, "command_type": record.command_type.value},
        )
        return record

    async def mark_cancelled(self, command_id: str) -> CommandRecord:
        return await self._transition(command_id, CommandStatus.CANCELLED)

    async def get(self, command_id: str) -> CommandRecord:
        async with self._lock:
            return await self._find(command_id)

    def cancellation_requested(self, command_id: str) -> bool:
        """Read cancellation without awaiting while the safety gate is held."""
        record = self._commands_by_id.get(command_id)
        if record is not None:
            return record.cancel_requested_at is not None
        if self._repo is None:
            return False
        raw = self._repo.get_by_command_id(command_id)
        return raw is not None and raw.get("cancel_requested_at") is not None
