from __future__ import annotations

import datetime as dt
import re
from enum import StrEnum
from typing import Any, Self, cast

from ..validation.modeling import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SECRET_TERMS = {
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
}
_FORBIDDEN_DATA_TERMS = {
    "audio_path",
    "filesystem_path",
    "payload",
    "raw_text",
    "script_text",
    "synthesis_text",
    "traceback",
}


class CommandContractError(ValueError):
    pass


class CommandTransitionError(CommandContractError):
    pass


class CommandType(StrEnum):
    CYCLE_REBUILD = "cycle.rebuild"
    SEGMENT_REFRESH = "segment.refresh"
    HEIGHTENED_SET = "mode.heightened.set"
    HEIGHTENED_CLEAR = "mode.heightened.clear"
    TEST_ORIGINATE = "tests.originate"
    INSERT_TEXT_CREATE = "inserts.text.create"
    INSERT_AUDIO_CREATE = "inserts.audio.create"
    INSERT_CANCEL = "inserts.cancel"
    ORIGINATE_TEXT = "originate.text"
    ORIGINATE_AUDIO = "originate.audio"
    CONFIG_RELOAD = "config.reload"
    AUDIO_UPLOAD = "audio.upload"


class CommandStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


TERMINAL_COMMAND_STATUSES = frozenset(
    {
        CommandStatus.SUCCEEDED,
        CommandStatus.FAILED,
        CommandStatus.CANCELLED,
        CommandStatus.EXPIRED,
        CommandStatus.SUPERSEDED,
    }
)


class RelationshipCompletion(StrEnum):
    NO_JOBS = "no_jobs"
    ALL_REQUIRED_JOBS = "all_required_jobs"
    CONTROLLER_FINALIZATION = "controller_finalization"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, use_enum_values=False)


def _require_utc(value: dt.datetime | None, field_name: str) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(dt.UTC)


def _required_utc(value: dt.datetime, field_name: str) -> dt.datetime:
    normalized = _require_utc(value, field_name)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def _validate_identifier(value: str, field_name: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded opaque identifier")
    return value


def _validate_bounded_json(value: Any, *, path: str = "details", depth: int = 0) -> Any:
    if depth > 4:
        raise ValueError(f"{path} exceeds maximum nesting depth")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value) > 512:
            raise ValueError(f"{path} contains an overlong string")
        return value
    if isinstance(value, list | tuple):
        return _validate_json_sequence(value, path=path, depth=depth)
    if isinstance(value, dict):
        return _validate_json_mapping(value, path=path, depth=depth)
    raise ValueError(f"{path} must be deterministic JSON data")


def _validate_json_sequence(value: list[Any] | tuple[Any, ...], *, path: str, depth: int) -> list[Any]:
    if len(value) > 32:
        raise ValueError(f"{path} contains too many items")
    return [_validate_bounded_json(item, path=f"{path}[]", depth=depth + 1) for item in value]


def _validate_json_mapping(value: dict[Any, Any], *, path: str, depth: int) -> dict[str, Any]:
    if len(value) > 32:
        raise ValueError(f"{path} contains too many keys")
    normalized: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = str(raw_key).strip().lower()
        if not _CODE_RE.fullmatch(key):
            raise ValueError(f"{path} contains an invalid key")
        if key in _SECRET_TERMS or key in _FORBIDDEN_DATA_TERMS:
            raise ValueError(f"{path} contains prohibited data")
        normalized[key] = _validate_bounded_json(item, path=f"{path}.{key}", depth=depth + 1)
    return normalized


class CommandAuditContext(ContractModel):
    channel: str = Field(min_length=1, max_length=32)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, value: str) -> str:
        if not _CODE_RE.fullmatch(value):
            raise ValueError("channel must be a declared bounded key")
        return value

    @field_validator("attributes")
    @classmethod
    def validate_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _validate_bounded_json(value, path="audit_context"))


class CommandResult(ContractModel):
    code: str = Field(min_length=2, max_length=64)
    message: str = Field(min_length=1, max_length=512)
    details: dict[str, Any] = Field(default_factory=dict)
    references: tuple[str, ...] = Field(default_factory=tuple, max_length=16)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not _CODE_RE.fullmatch(value):
            raise ValueError("result code must be a bounded declared key")
        return value

    @field_validator("details")
    @classmethod
    def validate_details(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _validate_bounded_json(value))

    @field_validator("references")
    @classmethod
    def validate_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_identifier(item, "result reference") for item in value)


class CommandError(ContractModel):
    code: str = Field(min_length=2, max_length=64)
    message: str = Field(min_length=1, max_length=512)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not _CODE_RE.fullmatch(value):
            raise ValueError("error code must be a bounded declared key")
        return value

    @field_validator("details")
    @classmethod
    def validate_details(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _validate_bounded_json(value))


class CommandRelationshipPolicy(ContractModel):
    completion: RelationshipCompletion
    required_job_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    optional_job_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    cancel_required_jobs: bool = True
    controller_finalization_required: bool = False

    @field_validator("required_job_ids", "optional_job_ids")
    @classmethod
    def validate_job_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("job relationship identifiers must be unique")
        return tuple(_validate_identifier(item, "job_id") for item in value)

    @model_validator(mode="after")
    def validate_relationship(self) -> Self:
        overlap = set(self.required_job_ids) & set(self.optional_job_ids)
        if overlap:
            raise ValueError("required and optional job identifiers must be disjoint")
        if self.completion is RelationshipCompletion.NO_JOBS and (self.required_job_ids or self.optional_job_ids):
            raise ValueError("no_jobs completion cannot declare child jobs")
        if self.completion is RelationshipCompletion.ALL_REQUIRED_JOBS and not self.required_job_ids:
            raise ValueError("all_required_jobs completion requires a child job")
        if (
            self.completion is RelationshipCompletion.CONTROLLER_FINALIZATION
            and not self.controller_finalization_required
        ):
            raise ValueError("controller finalization must be explicitly required")
        return self


class CommandRecord(ContractModel):
    command_id: str
    command_type: CommandType
    status: CommandStatus = CommandStatus.ACCEPTED
    actor: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_id: str
    correlation_id: str | None = None
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: dt.datetime
    accepted_at: dt.datetime
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    cancel_requested_at: dt.datetime | None = None
    idempotent_replay_count: int = Field(default=0, ge=0)
    result: CommandResult | None = None
    error: CommandError | None = None
    audit_context: CommandAuditContext
    relationship: CommandRelationshipPolicy

    @field_validator("command_id", "request_id", "correlation_id")
    @classmethod
    def validate_ids(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, info.field_name)

    @field_validator("actor", "reason", "idempotency_key")
    @classmethod
    def validate_safe_text(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        lowered = value.lower()
        if any(term in lowered for term in ("authorization:", "bearer ", "seasonalclient ")):
            raise ValueError(f"{info.field_name} contains prohibited credential material")
        if any(not char.isprintable() for char in value):
            raise ValueError(f"{info.field_name} contains unsupported characters")
        return value

    @field_validator(
        "created_at",
        "accepted_at",
        "started_at",
        "finished_at",
        "cancel_requested_at",
    )
    @classmethod
    def validate_timestamp(cls, value: dt.datetime | None, info: Any) -> dt.datetime | None:
        return _require_utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        _validate_command_timestamps(self)
        _validate_command_terminal_shape(self)
        return self

    def snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"payload_hash", "audit_context", "relationship"})


_COMMAND_TRANSITIONS: dict[CommandStatus, frozenset[CommandStatus]] = {
    CommandStatus.ACCEPTED: frozenset(
        {
            CommandStatus.RUNNING,
            CommandStatus.FAILED,
            CommandStatus.CANCELLED,
            CommandStatus.EXPIRED,
            CommandStatus.SUPERSEDED,
        }
    ),
    CommandStatus.RUNNING: frozenset(
        {
            CommandStatus.SUCCEEDED,
            CommandStatus.FAILED,
            CommandStatus.CANCELLED,
            CommandStatus.EXPIRED,
            CommandStatus.SUPERSEDED,
        }
    ),
}


def _validate_command_timestamps(command: CommandRecord) -> None:
    if command.accepted_at < command.created_at:
        raise ValueError("accepted_at cannot precede created_at")
    if command.started_at is not None and command.started_at < command.accepted_at:
        raise ValueError("started_at cannot precede accepted_at")
    if command.finished_at is not None and command.finished_at < (command.started_at or command.accepted_at):
        raise ValueError("finished_at cannot precede command activity")
    if command.cancel_requested_at is not None and command.cancel_requested_at < command.accepted_at:
        raise ValueError("cancel_requested_at cannot precede accepted_at")


def _validate_command_terminal_shape(command: CommandRecord) -> None:
    terminal = command.status in TERMINAL_COMMAND_STATUSES
    if command.status is CommandStatus.SUCCEEDED and command.result is None:
        raise ValueError("succeeded commands require a typed result")
    if command.status is CommandStatus.FAILED and command.error is None:
        raise ValueError("failed commands require a typed error")
    if terminal != (command.finished_at is not None):
        raise ValueError("finished_at must be present exactly for terminal commands")


def request_command_cancellation(command: CommandRecord, *, at: dt.datetime) -> CommandRecord:
    if command.status in TERMINAL_COMMAND_STATUSES:
        raise CommandTransitionError("terminal command cancellation cannot be requested")
    if command.cancel_requested_at is not None:
        return command
    timestamp = _required_utc(at, "cancel_requested_at")
    return command.model_copy(update={"cancel_requested_at": timestamp})


def transition_command(
    command: CommandRecord,
    target: CommandStatus,
    *,
    at: dt.datetime,
    result: CommandResult | None = None,
    error: CommandError | None = None,
) -> CommandRecord:
    if target is command.status:
        return command
    if command.status in TERMINAL_COMMAND_STATUSES:
        raise CommandTransitionError("terminal command status is immutable")
    if target not in _COMMAND_TRANSITIONS.get(command.status, frozenset()):
        raise CommandTransitionError(f"invalid command transition: {command.status.value} -> {target.value}")
    timestamp = _required_utc(at, "transition timestamp")
    updates = _command_transition_updates(target, timestamp)
    if target is CommandStatus.SUCCEEDED:
        if result is None:
            raise CommandTransitionError("command success requires a typed result")
        updates.update(result=result, error=None)
    elif target is CommandStatus.FAILED:
        if error is None:
            raise CommandTransitionError("command failure requires a typed error")
        updates.update(error=error, result=None)
    elif result is not None or error is not None:
        raise CommandTransitionError("result and error are valid only for matching terminal transitions")
    return CommandRecord.model_validate(command.model_dump() | updates)


def _command_transition_updates(target: CommandStatus, timestamp: dt.datetime) -> dict[str, Any]:
    updates: dict[str, Any] = {"status": target}
    if target is CommandStatus.RUNNING:
        updates["started_at"] = timestamp
    if target in TERMINAL_COMMAND_STATUSES:
        updates["finished_at"] = timestamp
    return updates


def _bounded_result_item(key: str, value: Any) -> tuple[str, Any] | None:
    if key in {"ok", "actor"} or key in _SECRET_TERMS or key in _FORBIDDEN_DATA_TERMS:
        return None
    if key.endswith("_path") or key.endswith("_text"):
        return None
    try:
        return key, _validate_bounded_json(value, path=f"result.{key}")
    except ValueError:
        return None


def _result_reference(key: str, value: Any) -> str | None:
    if key in _SECRET_TERMS or key in _FORBIDDEN_DATA_TERMS:
        return None
    if key.endswith("_id") and isinstance(value, str) and _ID_RE.fullmatch(value):
        return value
    return None


def command_result_from_mapping(raw: dict[str, Any]) -> CommandResult:
    details: dict[str, Any] = {}
    references: list[str] = []
    for raw_key, value in raw.items():
        key = str(raw_key).strip().lower()
        reference = _result_reference(key, value)
        if reference is not None:
            references.append(reference)
            continue
        bounded = _bounded_result_item(key, value)
        if bounded is not None:
            details[bounded[0]] = bounded[1]
    return CommandResult(
        code="completed",
        message="Command completed successfully.",
        details=details,
        references=tuple(dict.fromkeys(references))[:16],
    )


def command_error_from_mapping(raw: dict[str, Any]) -> CommandError:
    code = str(raw.get("code") or "command_failed").strip().lower()
    if not _CODE_RE.fullmatch(code):
        code = "command_failed"
    message = str(raw.get("message") or "Command failed.").strip()[:512] or "Command failed."
    details = raw.get("details")
    try:
        bounded_details = _validate_bounded_json(details if isinstance(details, dict) else {})
    except ValueError:
        bounded_details = {}
    return CommandError(code=code, message=message, details=bounded_details)
