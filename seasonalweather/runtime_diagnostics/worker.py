"""Controller translation for bounded simulated SWWP worker diagnostics."""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from seasonalweather.diagnostics import load_catalog
from seasonalweather.diagnostics.bindings import RUNTIME_CODES
from seasonalweather.diagnostics.namespaces import NAMESPACE_BY_TOKEN, NamespaceState
from seasonalweather.swwp.messages import (
    DiagnosticTransition,
    WorkerDiagnostic,
    WorkerDiagnosticAck,
)

from .models import CorrelationContext, DiagnosticRole, PromotionReason
from .redaction import redact_text
from .service import RuntimeDiagnosticService

WORKER_DIAGNOSTIC_ENVELOPE_VERSION = 1
MAX_WORKER_RELATIONSHIPS = 1_024
RelationshipKey = tuple[str, str, str, int, str]
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_RESERVED_NAMESPACES = frozenset(
    token for token, namespace in NAMESPACE_BY_TOKEN.items() if namespace.state is NamespaceState.RESERVED
)


@dataclass
class _Relationship:
    activation_signature: str
    activation_ack: WorkerDiagnosticAck
    occurrence_id: str
    resolution_signature: str | None = None
    resolution_ack: WorkerDiagnosticAck | None = None
    active: bool = True


@dataclass
class WorkerDiagnosticTranslator:
    service: RuntimeDiagnosticService
    controller_instance_id: str
    _relationships: OrderedDict[RelationshipKey, _Relationship] = field(default_factory=OrderedDict)
    _owners: dict[str, set[RelationshipKey]] = field(default_factory=dict)

    @property
    def relationship_count(self) -> int:
        return len(self._relationships)

    def release_session(
        self,
        *,
        worker_id: str,
        worker_instance_id: str,
        session_id: str,
        worker_epoch: int,
    ) -> None:
        prefix = (worker_id, worker_instance_id, session_id, worker_epoch)
        for key, relationship in tuple(self._relationships.items()):
            if key[:4] != prefix:
                continue
            owners = self._owners.get(relationship.occurrence_id)
            if owners is not None:
                owners.discard(key)
                if not owners:
                    self._owners.pop(relationship.occurrence_id, None)
            del self._relationships[key]

    def handle(
        self,
        diagnostic: WorkerDiagnostic,
        *,
        worker_id: str,
        worker_instance_id: str,
        session_id: str,
        worker_epoch: int,
    ) -> WorkerDiagnosticAck:
        diagnostic_id = _safe_diagnostic_id(diagnostic)
        try:
            key = _relationship_key(
                diagnostic_id=diagnostic_id,
                worker_id=worker_id,
                worker_instance_id=worker_instance_id,
                session_id=session_id,
                worker_epoch=worker_epoch,
            )
            if diagnostic.transition is DiagnosticTransition.RESOLVED:
                return self._resolve(diagnostic, key=key)
            return self._activate(
                diagnostic,
                key=key,
                worker_id=worker_id,
                session_id=session_id,
            )
        except Exception:
            self._safe_rejection(
                diagnostic,
                worker_id=worker_id,
                reason_code="unsafe_diagnostic_input",
                message="Worker diagnostic input was rejected safely.",
            )
            return WorkerDiagnosticAck(
                diagnostic_id=diagnostic_id,
                accepted=False,
                summary="worker diagnostic input was rejected safely",
            )

    def _activate(
        self,
        diagnostic: WorkerDiagnostic,
        *,
        key: RelationshipKey,
        worker_id: str,
        session_id: str,
    ) -> WorkerDiagnosticAck:
        signature = _signature(diagnostic)
        prior = self._relationships.get(key)
        if prior is not None:
            self._relationships.move_to_end(key)
            if prior.activation_signature == signature:
                return prior.activation_ack
            return self._contradictory_reuse(diagnostic, worker_id=worker_id)
        if not self._make_room():
            self._safe_rejection(
                diagnostic,
                worker_id=worker_id,
                reason_code="relationship_limit",
                message="Worker diagnostic relationship capacity was exhausted.",
            )
            return WorkerDiagnosticAck(
                diagnostic_id=diagnostic.diagnostic_id,
                accepted=False,
                summary="worker diagnostic relationship capacity was exhausted",
            )

        acknowledgment = self._activate_new(
            diagnostic,
            worker_id=worker_id,
            session_id=session_id,
        )
        if acknowledgment.accepted and acknowledgment.controller_occurrence_id is not None:
            relationship = _Relationship(
                activation_signature=signature,
                activation_ack=acknowledgment,
                occurrence_id=acknowledgment.controller_occurrence_id,
            )
            self._relationships[key] = relationship
            self._owners.setdefault(relationship.occurrence_id, set()).add(key)
        return acknowledgment

    def _activate_new(
        self,
        diagnostic: WorkerDiagnostic,
        *,
        worker_id: str,
        session_id: str,
    ) -> WorkerDiagnosticAck:
        if _reserved_namespace(diagnostic.code) is not None:
            self._safe_rejection(
                diagnostic,
                worker_id=worker_id,
                reason_code="reserved_diagnostic_namespace",
                message="Worker diagnostic code uses a reserved namespace.",
            )
            return WorkerDiagnosticAck(
                diagnostic_id=diagnostic.diagnostic_id,
                accepted=False,
                summary="worker diagnostic code uses a reserved namespace",
            )
        catalog = load_catalog()
        compatible_schema = diagnostic.diagnostic_schema_version == catalog.diagnostic_schema_version
        compatible_envelope = diagnostic.envelope_schema_version == WORKER_DIAGNOSTIC_ENVELOPE_VERSION
        compatible_catalog = diagnostic.catalog_version == catalog.diagnostic_catalog_version
        if not compatible_schema or not compatible_envelope or not compatible_catalog:
            return self._compatibility(
                diagnostic,
                worker_id=worker_id,
                session_id=session_id,
            )
        definition = catalog.definition(diagnostic.code)
        if definition is None:
            self._safe_rejection(
                diagnostic,
                worker_id=worker_id,
                reason_code="unknown_matching_catalog_code",
                message="Worker diagnostic code is absent from the matching controller catalog.",
            )
            return WorkerDiagnosticAck(
                diagnostic_id=diagnostic.diagnostic_id,
                accepted=False,
                summary="worker diagnostic code is absent from the matching controller catalog",
            )
        context = CorrelationContext(
            role=DiagnosticRole.WORKER,
            instance_id=self.controller_instance_id,
            component=diagnostic.component,
            worker_id=worker_id,
            swwp_session_id=session_id,
            capability=diagnostic.capability,
            reason_code=diagnostic.reason_code,
        )
        instance = self.service.build(
            code=diagnostic.code,
            context=context,
            message=diagnostic.short_message,
            operational_effect=definition.summary,
            recovery_action="Apply the controller catalog guidance for this worker condition.",
            promotion_reason=PromotionReason.OPERATOR_ATTENTION,
            exception_evidence=_worker_evidence(diagnostic),
        )
        result = self.service.promote(instance)
        return WorkerDiagnosticAck(
            diagnostic_id=diagnostic.diagnostic_id,
            accepted=True,
            controller_occurrence_id=result.occurrence.occurrence_id,
            summary="worker diagnostic accepted under controller policy",
        )

    def _resolve(
        self,
        diagnostic: WorkerDiagnostic,
        *,
        key: RelationshipKey,
    ) -> WorkerDiagnosticAck:
        relationship = self._relationships.get(key)
        signature = _signature(diagnostic)
        if relationship is None:
            return self._unauthorized_resolution(diagnostic, worker_id=key[0])
        self._relationships.move_to_end(key)
        if relationship.resolution_signature is not None:
            if relationship.resolution_signature == signature:
                if relationship.resolution_ack is None:
                    raise ValueError("resolved relationship is missing its acknowledgment")
                return relationship.resolution_ack
            return self._contradictory_reuse(diagnostic, worker_id=key[0])
        if not relationship.active or relationship.occurrence_id != diagnostic.controller_occurrence_id:
            return self._unauthorized_resolution(diagnostic, worker_id=key[0])

        owners = self._owners.get(relationship.occurrence_id, set())
        remaining = owners - {key}
        if remaining:
            acknowledgment = WorkerDiagnosticAck(
                diagnostic_id=diagnostic.diagnostic_id,
                accepted=True,
                controller_occurrence_id=relationship.occurrence_id,
                summary="worker relationship resolved; occurrence remains active for another worker",
            )
        else:
            resolved = self.service.resolve(
                relationship.occurrence_id,
                reason=diagnostic.short_message,
                evidence={"worker_diagnostic_id": diagnostic.diagnostic_id},
            )
            if resolved is None:
                return WorkerDiagnosticAck(
                    diagnostic_id=diagnostic.diagnostic_id,
                    accepted=False,
                    summary="worker occurrence is unavailable",
                )
            acknowledgment = WorkerDiagnosticAck(
                diagnostic_id=diagnostic.diagnostic_id,
                accepted=True,
                controller_occurrence_id=relationship.occurrence_id,
                summary="worker diagnostic resolved",
            )

        relationship.active = False
        relationship.resolution_signature = signature
        relationship.resolution_ack = acknowledgment
        if remaining:
            self._owners[relationship.occurrence_id] = remaining
        else:
            self._owners.pop(relationship.occurrence_id, None)
        self._relationships.move_to_end(key)
        return acknowledgment

    def _compatibility(
        self,
        diagnostic: WorkerDiagnostic,
        *,
        worker_id: str,
        session_id: str,
    ) -> WorkerDiagnosticAck:
        context = CorrelationContext(
            role=DiagnosticRole.CONTROLLER,
            instance_id=self.controller_instance_id,
            component="swwp-diagnostics",
            worker_id=worker_id,
            swwp_session_id=session_id,
            reason_code="diagnostic_compatibility",
        )
        instance = self.service.build(
            code=RUNTIME_CODES["worker_diagnostic_incompatible"],
            context=context,
            message=redact_text(
                f"Worker diagnostic {diagnostic.code}: {diagnostic.short_message}",
                limit=512,
            ),
            operational_effect="Worker diagnostic semantics could not be resolved through the local catalog.",
            recovery_action="Align worker and controller diagnostic schema and catalog versions.",
            promotion_reason=PromotionReason.DEGRADATION,
            exception_evidence=_worker_evidence(diagnostic),
        )
        result = self.service.promote(instance)
        return WorkerDiagnosticAck(
            diagnostic_id=diagnostic.diagnostic_id,
            accepted=True,
            controller_occurrence_id=result.occurrence.occurrence_id,
            compatibility=True,
            summary="bounded opaque worker evidence retained under compatibility occurrence",
        )

    def _unauthorized_resolution(
        self,
        diagnostic: WorkerDiagnostic,
        *,
        worker_id: str,
    ) -> WorkerDiagnosticAck:
        self._safe_rejection(
            diagnostic,
            worker_id=worker_id,
            reason_code="unauthorized_resolution",
            message="Worker diagnostic resolution did not own the controller occurrence relationship.",
        )
        return WorkerDiagnosticAck(
            diagnostic_id=diagnostic.diagnostic_id,
            accepted=False,
            summary="worker resolution does not own the controller occurrence relationship",
        )

    def _contradictory_reuse(
        self,
        diagnostic: WorkerDiagnostic,
        *,
        worker_id: str,
    ) -> WorkerDiagnosticAck:
        self._safe_rejection(
            diagnostic,
            worker_id=worker_id,
            reason_code="contradictory_diagnostic_reuse",
            message="Worker diagnostic ID was reused with contradictory content.",
        )
        return WorkerDiagnosticAck(
            diagnostic_id=diagnostic.diagnostic_id,
            accepted=False,
            summary="worker diagnostic ID was reused with contradictory content",
        )

    def _safe_rejection(
        self,
        diagnostic: WorkerDiagnostic,
        *,
        worker_id: str,
        reason_code: str,
        message: str,
    ) -> None:
        try:
            instance = self.service.build(
                code=RUNTIME_CODES["worker_diagnostic_rejected"],
                context=CorrelationContext(
                    role=DiagnosticRole.CONTROLLER,
                    instance_id=self.controller_instance_id,
                    component="swwp-diagnostics",
                    worker_id=worker_id,
                    reason_code=reason_code,
                ),
                message=message,
                operational_effect="The worker input did not mutate controller occurrence state.",
                recovery_action="Send a fresh bounded diagnostic under the current worker session.",
                promotion_reason=PromotionReason.OPERATOR_ATTENTION,
            )
            self.service.promote(instance)
        except Exception:
            return

    def _make_room(self) -> bool:
        if len(self._relationships) < MAX_WORKER_RELATIONSHIPS:
            return True
        for key, relationship in tuple(self._relationships.items()):
            if not relationship.active:
                del self._relationships[key]
                if len(self._relationships) < MAX_WORKER_RELATIONSHIPS:
                    return True
        return False


def _relationship_key(
    *,
    diagnostic_id: str,
    worker_id: str,
    worker_instance_id: str,
    session_id: str,
    worker_epoch: int,
) -> RelationshipKey:
    for value, name in (
        (worker_id, "worker_id"),
        (worker_instance_id, "worker_instance_id"),
        (session_id, "session_id"),
    ):
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError(f"{name} is invalid")
    if not 1 <= worker_epoch <= 2_147_483_647:
        raise ValueError("worker_epoch is invalid")
    return (worker_id, worker_instance_id, session_id, worker_epoch, diagnostic_id)


def _safe_diagnostic_id(diagnostic: WorkerDiagnostic) -> str:
    value = getattr(diagnostic, "diagnostic_id", None)
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else "diagnostic_rejected"


def _signature(diagnostic: WorkerDiagnostic) -> str:
    payload = diagnostic.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _worker_evidence(diagnostic: WorkerDiagnostic) -> dict[str, Any] | None:
    if diagnostic.evidence is None:
        return None
    evidence = diagnostic.evidence
    return {
        "type": redact_text(evidence.exception_type, limit=256),
        "message": redact_text(evidence.message, limit=1024),
        "notes": [redact_text(note, limit=512) for note in evidence.notes],
        "frames": [
            {
                "filename": _worker_frame_filename(frame.filename),
                "line": frame.line,
                "function": redact_text(frame.function, limit=256),
                "source": redact_text(frame.source, limit=512),
            }
            for frame in evidence.frames
        ],
    }


def _reserved_namespace(code: object) -> str | None:
    if not isinstance(code, str):
        return None
    return next((token for token in _RESERVED_NAMESPACES if code.startswith(token)), None)


def _worker_frame_filename(value: str) -> str:
    basename = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if basename in {"", ".", ".."}:
        basename = "[worker-frame]"
    return redact_text(basename, limit=128)
