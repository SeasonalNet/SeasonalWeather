"""Thin controller compatibility facade for API application services."""

from __future__ import annotations

from typing import Any

from .application.errors import ConflictError, ControlError, DependencyUnavailableError, NotFoundError
from .application.read_models import RuntimeReadModelService
from .artifacts.audio_assets import AudioAssetService
from .broadcast.cycle_insert_service import CycleInsertService
from .broadcast.manual_api_service import ManualOriginationService
from .broadcast.operator_service import BroadcastOperatorService
from .broadcast.segment_service import SegmentApplicationService, SegmentServiceError

__all__ = [
    "ConflictError",
    "ControlError",
    "DependencyUnavailableError",
    "NotFoundError",
    "OrchestratorControl",
]


class OrchestratorControl:
    """Compatibility facade that composes packet-owned application services."""

    def __init__(
        self,
        orch: Any,
        *,
        config_path: str,
        segment_service: SegmentApplicationService | None = None,
    ) -> None:
        self.orch = orch
        self.config_path = str(config_path)
        self.segment_service = segment_service
        self._read_models = RuntimeReadModelService(orch, config_path=self.config_path)
        self._audio_assets = AudioAssetService(orch)
        self._broadcast_operations = BroadcastOperatorService(orch)
        self._manual_origination = ManualOriginationService(orch, self._audio_assets)
        self._cycle_inserts = CycleInsertService(orch, self._audio_assets)

    @staticmethod
    def _segment_service_error(exc: SegmentServiceError) -> ControlError:
        return ControlError(exc.code, exc.message, status_code=exc.status_code)

    async def list_segments(self) -> dict[str, Any]:
        if self.segment_service is None:
            raise ControlError("segments_unavailable", "Segment inspection is unavailable.", status_code=503)
        return self.segment_service.list_segments()

    async def get_segment(self, key: str) -> dict[str, Any]:
        if self.segment_service is None:
            raise ControlError("segments_unavailable", "Segment inspection is unavailable.", status_code=503)
        try:
            return self.segment_service.get_segment(key)
        except SegmentServiceError as exc:
            raise self._segment_service_error(exc) from exc

    async def get_cycle_plan(self) -> dict[str, Any]:
        if self.segment_service is None:
            raise ControlError("segments_unavailable", "Segment inspection is unavailable.", status_code=503)
        return self.segment_service.cycle_plan()

    async def get_cycle_preview(self) -> dict[str, Any]:
        if self.segment_service is None:
            raise ControlError("segments_unavailable", "Segment inspection is unavailable.", status_code=503)
        return self.segment_service.cycle_preview()

    async def refresh_segment(
        self,
        *,
        key: str,
        actor: str,
        idempotency_key: str,
        command_store: Any,
        request_id: str | None = None,
    ) -> tuple[Any, bool]:
        if self.segment_service is None:
            raise ControlError("segments_unavailable", "Segment refresh is unavailable.", status_code=503)
        try:
            return await self.segment_service.accept_refresh(
                key=key,
                actor=actor,
                idempotency_key=idempotency_key,
                command_store=command_store,
                request_id=request_id,
            )
        except SegmentServiceError as exc:
            raise self._segment_service_error(exc) from exc

    async def get_health(self) -> dict[str, Any]:
        return await self._read_models.health()

    async def get_status(self) -> dict[str, Any]:
        return await self._read_models.status()

    async def get_station_feed(self, *, missing_ok: bool = False) -> dict[str, Any]:
        return await self._read_models.station_feed(missing_ok=missing_ok)

    async def get_public_handled_alerts(self) -> dict[str, Any]:
        return await self._read_models.public_handled_alerts()

    async def get_config_summary(self) -> dict[str, Any]:
        return await self._read_models.config_summary()

    async def get_config_schema(self) -> dict[str, object]:
        return await self._read_models.config_schema()

    async def get_effective_config(self) -> dict[str, object]:
        return await self._read_models.effective_config()

    async def validate_config(
        self,
        *,
        preflight: bool = False,
        warnings_as_errors: bool = False,
    ) -> dict[str, object]:
        return await self._read_models.validate_config(
            preflight=preflight,
            warnings_as_errors=warnings_as_errors,
        )

    async def rebuild_cycle(self, *, reason: str | None, actor: str) -> dict[str, Any]:
        return await self._broadcast_operations.rebuild_cycle(reason=reason, actor=actor)

    async def set_heightened_mode(self, *, minutes: int, reason: str, actor: str) -> dict[str, Any]:
        return await self._broadcast_operations.set_heightened_mode(minutes=minutes, reason=reason, actor=actor)

    async def clear_heightened_mode(self, *, reason: str | None, actor: str) -> dict[str, Any]:
        return await self._broadcast_operations.clear_heightened_mode(reason=reason, actor=actor)

    async def originate_test(self, *, event_code: str, actor: str) -> dict[str, Any]:
        return await self._broadcast_operations.originate_test(event_code=event_code, actor=actor)

    def audio_upload_max_bytes(self) -> int:
        return self._audio_assets.upload_max_bytes()

    async def stage_wav_upload(self, *, filename: str, content_type: str, data: bytes, actor: str) -> dict[str, Any]:
        return await self._audio_assets.stage_upload(
            filename=filename,
            content_type=content_type,
            data=data,
            actor=actor,
        )

    async def originate_text(self, req: Any, *, actor: str) -> dict[str, Any]:
        return await self._manual_origination.originate_text(req, actor=actor)

    async def originate_audio(self, req: Any, *, actor: str) -> dict[str, Any]:
        return await self._manual_origination.originate_audio(req, actor=actor)

    async def create_text_insert(self, req: Any, *, actor: str) -> dict[str, Any]:
        return await self._cycle_inserts.create_text(req, actor=actor)

    async def create_audio_insert(self, req: Any, *, actor: str) -> dict[str, Any]:
        return await self._cycle_inserts.create_audio(req, actor=actor)

    async def list_inserts(self, *, include_inactive: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        return self._cycle_inserts.list(include_inactive=include_inactive, limit=limit)

    async def get_insert(self, insert_id: str) -> dict[str, Any]:
        return self._cycle_inserts.get(insert_id)

    async def cancel_insert(self, insert_id: str, *, actor: str) -> dict[str, Any]:
        return self._cycle_inserts.cancel(insert_id, actor=actor)
