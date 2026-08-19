from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..auth.service import AuthenticationError, AuthenticationService
from ..configuration_reload.models import ReloadRequest, WarningAcknowledgment
from ..control import ControlError, OrchestratorControl
from ..health_service import (
    ComponentProbe,
    ComponentState,
    HealthComponent,
    HealthService,
)
from ..lifecycle import Lifecycle, WorkClass
from .auth import ApiPrincipal, get_client_authentication, require_route_policy
from .commands import CommandNotFoundError, CommandStore, IdempotencyConflictError
from .models import (
    AudioUploadAccepted,
    ClearHeightenedModeRequest,
    CommandAccepted,
    CommandSnapshot,
    ConfigReloadRequest,
    CreateAudioInsertRequest,
    CreateTextInsertRequest,
    CycleInsertList,
    CycleInsertSnapshot,
    CyclePlanResponse,
    CyclePreviewResponse,
    OriginateAudioRequest,
    OriginateTestRequest,
    OriginateTextRequest,
    ProblemDetails,
    RebuildCycleRequest,
    SegmentListResponse,
    SegmentSnapshot,
    SetHeightenedModeRequest,
    TokenExchangeRequest,
    TokenExchangeResponse,
    TokenRevocationRequest,
    TokenRevocationResponse,
)
from .openapi import (
    API_VERSION,
    PROBLEM_JSON,
    PUBLIC_PROBLEM_RESPONSES,
    STANDARD_PROBLEM_RESPONSES,
    install_openapi,
    json_response,
)

_CODE_RE = re.compile(r"[^a-z0-9_-]+")


def _new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


def _request_id(request: Request) -> str:
    header = (request.headers.get("x-request-id") or "").strip()
    if header and len(header) <= 128 and all(ch.isprintable() and not ch.isspace() for ch in header):
        return header
    return _new_request_id()


def _status_title(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "HTTP error"


def _problem_type(code: str) -> str:
    slug = _CODE_RE.sub("-", code.strip().lower().replace("_", "-")).strip("-")
    return f"/problems/{slug or 'http-error'}"


def _problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    detail: str,
    title: str | None = None,
    details: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    response_headers = dict(headers or {})
    response_headers["X-Request-ID"] = request_id
    response_headers.setdefault("Cache-Control", "no-store")

    payload = ProblemDetails(
        type=_problem_type(code),
        title=title or _status_title(status_code),
        status=status_code,
        detail=detail,
        instance=str(request.url.path),
        code=code,
        details=details or {},
        errors=errors,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json", exclude_none=True),
        media_type=PROBLEM_JSON,
        headers=response_headers,
    )


def _command_accepted(record: Any, *, replayed: bool) -> CommandAccepted:
    return CommandAccepted(
        command_id=record.command_id,
        command_type=record.command_type,
        status=record.status,
        accepted_at=record.accepted_at,
        idempotent_replay=replayed,
        request_id=record.request_id,
        status_url=f"/v1/commands/{record.command_id}",
    )


def _command_snapshot(record: Any) -> CommandSnapshot:
    return CommandSnapshot.model_validate(record.snapshot())


async def _require_idempotency_key(idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    key = (idempotency_key or "").strip()
    if not key:
        raise HTTPException(
            status_code=400,
            detail={"code": "missing_idempotency_key", "message": "Idempotency-Key header is required."},
        )
    if len(key) > 200:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_idempotency_key", "message": "Idempotency-Key is too long."}
        )
    return key


async def _execute_command(
    *,
    store: CommandStore,
    principal: ApiPrincipal,
    idempotency_key: str,
    command_type: str,
    payload: dict[str, Any],
    action: Callable[[], Awaitable[dict[str, Any]]],
    success_event: str | None = None,
) -> CommandAccepted:
    try:
        record, replayed = await store.create_or_replay(
            command_type=command_type,
            idempotency_key=idempotency_key,
            actor=principal.subject,
            payload=payload,
            reason=str(payload.get("reason")) if payload.get("reason") else None,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict", "message": str(exc)}) from exc

    if replayed:
        return _command_accepted(record, replayed=True)

    record = await store.mark_running(record.command_id)
    try:
        result = await action()
    except ControlError as exc:
        await store.mark_failed(record.command_id, exc.to_dict())
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc
    except Exception as exc:
        err = {"code": "internal_error", "message": "Unhandled server error while executing command."}
        await store.mark_failed(record.command_id, err)
        raise HTTPException(status_code=500, detail=err) from exc

    record = await store.mark_succeeded(record.command_id, result)
    if success_event:
        await store.broker.publish(
            success_event, {"command_id": record.command_id, "command_type": record.command_type, "result": result}
        )
    return _command_accepted(record, replayed=False)


async def _admit_async_command(
    *,
    store: CommandStore,
    principal: ApiPrincipal,
    idempotency_key: str,
    command_type: str,
    payload: dict[str, Any],
    admission: Callable[[], Awaitable[dict[str, Any]]],
) -> CommandAccepted:
    try:
        record, replayed = await store.create_or_replay(
            command_type=command_type,
            idempotency_key=idempotency_key,
            actor=principal.subject,
            payload=payload,
            reason=str(payload.get("reason")) if payload.get("reason") else None,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict", "message": str(exc)}) from exc
    if replayed:
        return _command_accepted(record, replayed=True)
    try:
        await admission()
    except ControlError as exc:
        await store.mark_failed(record.command_id, exc.to_dict())
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc
    except Exception as exc:
        error = {"code": "admission_failed", "message": "Asynchronous work could not be admitted."}
        await store.mark_failed(record.command_id, error)
        raise HTTPException(status_code=500, detail=error) from exc
    return _command_accepted(record, replayed=False)


def _detail_code_message(detail: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(detail, dict):
        code = str(detail.get("code") or "http_error")
        message = str(detail.get("message") or detail.get("detail") or "HTTP error")
        raw_details = detail.get("details")
        details = raw_details if isinstance(raw_details, dict) else {}
        return code, message, details
    return "http_error", str(detail or "HTTP error"), {}


def create_app(
    control: OrchestratorControl,
    *,
    store: CommandStore | None = None,
    auth_service: AuthenticationService | None = None,
    health_service: HealthService | None = None,
    lifecycle: Lifecycle | None = None,
    reload_service: Any | None = None,
) -> FastAPI:
    command_store = store or CommandStore()
    if health_service is None:

        async def runtime_unavailable() -> HealthComponent:
            return HealthComponent(
                "runtime",
                ComponentState.UNAVAILABLE,
                True,
                "health_service_unavailable",
            )

        health_service = HealthService([ComponentProbe("runtime", True, runtime_unavailable)])
    app = FastAPI(
        title="SeasonalWeather API",
        version=API_VERSION,
        openapi_version="3.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        swagger_ui_parameters={"defaultModelsExpandDepth": 1},
    )
    app.state.control = control
    app.state.command_store = command_store
    app.state.auth_service = auth_service
    app.state.health_service = health_service
    app.state.lifecycle = lifecycle
    app.state.reload_service = reload_service
    install_openapi(app)

    @app.middleware("http")
    async def _lifecycle_admission(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if (
            lifecycle is not None
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and not lifecycle.allows(WorkClass.COMMAND)
        ):
            return _problem_response(
                request,
                status_code=503,
                code="service_draining",
                detail="The service is draining and is not accepting mutable work.",
                headers={"Retry-After": "5"},
            )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = jsonable_encoder(exc.errors())
        for error in errors:
            error.pop("input", None)
        return _problem_response(
            request,
            status_code=422,
            code="request_validation_failed",
            detail="Request body, path, query, or header validation failed.",
            details={"errors": errors},
            errors=errors,
        )

    @app.exception_handler(HTTPException)
    async def _handle_fastapi_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        code, message, details = _detail_code_message(exc.detail)
        return _problem_response(
            request,
            status_code=exc.status_code,
            code=code,
            detail=message,
            details=details,
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_starlette_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message, details = _detail_code_message(exc.detail)
        if code == "http_error":
            code = "not_found" if exc.status_code == 404 else "http_error"
        return _problem_response(
            request,
            status_code=exc.status_code,
            code=code,
            detail=message if message != "HTTP error" else _status_title(exc.status_code),
            details=details,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        return _problem_response(
            request,
            status_code=500,
            code="internal_error",
            detail="Unhandled server error.",
        )

    @app.post(
        "/v1/auth/token",
        response_model=TokenExchangeResponse,
        tags=["authentication"],
        summary="Exchange a client credential for a short-lived access token.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_auth_token(
        request: Request,
        response: Response,
        body: TokenExchangeRequest,
        authentication: tuple[AuthenticationService, str, str] = Depends(get_client_authentication),
    ) -> TokenExchangeResponse:
        service, credential, client_host = authentication
        try:
            issued = service.issue_token(
                client_credential=credential,
                source_ip=client_host,
                requested_scopes=body.scopes,
                requested_ttl=body.ttl_seconds,
                request_id=_request_id(request),
            )
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
                headers={"WWW-Authenticate": "SeasonalClient"} if exc.status_code == 401 else None,
            ) from exc
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return TokenExchangeResponse(
            access_token=issued.access_token,
            expires_in=issued.expires_in,
            scopes=list(issued.scopes),
        )

    @app.post(
        "/v1/auth/revoke",
        response_model=TokenRevocationResponse,
        tags=["authentication"],
        summary="Revoke an access token owned by the calling client.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_auth_revoke(
        request: Request,
        body: TokenRevocationRequest,
        authentication: tuple[AuthenticationService, str, str] = Depends(get_client_authentication),
    ) -> TokenRevocationResponse:
        service, credential, client_host = authentication
        try:
            service.revoke_token(
                client_credential=credential,
                target_token=body.token,
                source_ip=client_host,
                request_id=_request_id(request),
            )
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
                headers={"WWW-Authenticate": "SeasonalClient"} if exc.status_code == 401 else None,
            ) from exc
        return TokenRevocationResponse()

    @app.get(
        "/healthz",
        tags=["status"],
        summary="Return minimal process liveness.",
        responses={
            200: json_response(
                "The ASGI application can answer requests.",
                {"$ref": "#/components/schemas/Liveness"},
            ),
            **PUBLIC_PROBLEM_RESPONSES,
        },
    )
    async def healthz(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        return {"status": "alive"}

    @app.get(
        "/readyz",
        tags=["status"],
        summary="Return broadcast-critical operational readiness.",
        responses={
            200: json_response(
                "The configured runtime is ready.",
                {"$ref": "#/components/schemas/Readiness"},
            ),
            503: json_response(
                "One or more required components are unavailable.",
                {"$ref": "#/components/schemas/Readiness"},
            ),
            **PUBLIC_PROBLEM_RESPONSES,
        },
    )
    async def readyz() -> JSONResponse:
        report = await health_service.collect()
        return JSONResponse(
            status_code=200 if report.ready else 503,
            content=report.to_dict(detailed=False),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/health",
        tags=["status"],
        summary="Return bounded detailed runtime health.",
        responses={
            200: json_response(
                "Detailed health report.",
                {"$ref": "#/components/schemas/DetailedHealth"},
            ),
            **STANDARD_PROBLEM_RESPONSES,
        },
    )
    async def v1_health(
        response: Response,
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/health")),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        report = await health_service.collect()
        return report.to_dict(detailed=True)

    @app.get(
        "/v1/status",
        tags=["status"],
        summary="Return runtime status for the station automation process.",
        responses={
            200: json_response("Runtime status payload.", {"$ref": "#/components/schemas/RuntimeStatus"}),
            **STANDARD_PROBLEM_RESPONSES,
        },
    )
    async def v1_status(
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/status")),
    ) -> dict[str, Any]:
        return await control.get_status()

    @app.get(
        "/v1/handled-alerts",
        tags=["station-feed"],
        summary="Return the public handled-alerts station feed.",
        responses={
            200: json_response("Station handled-alerts feed.", {"$ref": "#/components/schemas/StationFeed"}),
            **PUBLIC_PROBLEM_RESPONSES,
        },
    )
    async def v1_handled_alerts(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "public, max-age=2, stale-while-revalidate=30"
        return await control.get_public_handled_alerts()

    @app.get(
        "/v1/station-feed",
        tags=["station-feed"],
        summary="Return the authenticated station feed read model.",
        responses={
            200: json_response("Station feed payload.", {"$ref": "#/components/schemas/StationFeed"}),
            **STANDARD_PROBLEM_RESPONSES,
        },
    )
    async def v1_station_feed(
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/station-feed")),
    ) -> dict[str, Any]:
        return await control.get_station_feed()

    @app.get(
        "/v1/config/summary",
        tags=["configuration"],
        summary="Return a safe runtime configuration summary.",
        responses={
            200: json_response("Configuration summary payload.", {"$ref": "#/components/schemas/ConfigSummary"}),
            **STANDARD_PROBLEM_RESPONSES,
        },
    )
    async def v1_config_summary(
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/config/summary")),
    ) -> dict[str, Any]:
        return await control.get_config_summary()

    @app.get(
        "/v1/segments",
        response_model=SegmentListResponse,
        tags=["segments"],
        summary="List authoritative static segments and runtime provenance.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_segments(
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/segments")),  # noqa: B008
    ) -> SegmentListResponse:
        try:
            return SegmentListResponse.model_validate(await control.list_segments())
        except ControlError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc

    @app.get(
        "/v1/segments/{key}",
        response_model=SegmentSnapshot,
        tags=["segments"],
        summary="Inspect one authoritative segment and its bounded provenance.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_segment(
        key: str,
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/segments/{key}")),  # noqa: B008
    ) -> SegmentSnapshot:
        try:
            return SegmentSnapshot.model_validate(await control.get_segment(key))
        except ControlError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc

    @app.post(
        "/v1/segments/{key}/refresh",
        response_model=CommandAccepted,
        status_code=202,
        tags=["segments"],
        summary="Accept an asynchronous refresh for one independently buildable segment.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_segment_refresh(
        request: Request,
        key: str,
        principal: ApiPrincipal = Depends(require_route_policy("POST", "/v1/segments/{key}/refresh")),  # noqa: B008
        idempotency_key: str = Depends(_require_idempotency_key),
    ) -> CommandAccepted:
        try:
            record, replayed = await control.refresh_segment(
                key=key,
                actor=principal.subject,
                idempotency_key=idempotency_key,
                command_store=command_store,
                request_id=_request_id(request),
            )
        except ControlError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc
        return _command_accepted(record, replayed=replayed)

    @app.get(
        "/v1/cycle/plan",
        response_model=CyclePlanResponse,
        tags=["segments"],
        summary="Return the deterministic current normal and focus cycle plan.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_cycle_plan(
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/cycle/plan")),  # noqa: B008
    ) -> CyclePlanResponse:
        try:
            return CyclePlanResponse.model_validate(await control.get_cycle_plan())
        except ControlError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc

    @app.get(
        "/v1/cycle/preview",
        response_model=CyclePreviewResponse,
        tags=["segments"],
        summary="Preview the current cycle selection without side effects.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_cycle_preview(
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/cycle/preview")),  # noqa: B008
    ) -> CyclePreviewResponse:
        try:
            return CyclePreviewResponse.model_validate(await control.get_cycle_preview())
        except ControlError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc

    @app.get(
        "/v1/commands/{command_id}",
        response_model=CommandSnapshot,
        tags=["commands"],
        summary="Return a command snapshot by command ID.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_command(
        command_id: str,
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/commands/{command_id}")),
    ) -> CommandSnapshot:
        try:
            record = await command_store.get(command_id)
        except CommandNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail={"code": "command_not_found", "message": "Command was not found."}
            ) from exc
        return _command_snapshot(record)

    @app.post(
        "/v1/cycle/rebuild",
        response_model=CommandAccepted,
        status_code=202,
        tags=["control"],
        summary="Rebuild the normal station cycle.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_cycle_rebuild(
        req: RebuildCycleRequest,
        principal: ApiPrincipal = Depends(require_route_policy("POST", "/v1/cycle/rebuild")),
        idempotency_key: str = Depends(_require_idempotency_key),
    ) -> CommandAccepted:
        payload = req.model_dump(mode="json")
        return await _admit_async_command(
            store=command_store,
            principal=principal,
            idempotency_key=idempotency_key,
            command_type="cycle.rebuild",
            payload=payload,
            admission=lambda: control.rebuild_cycle(reason=req.reason, actor=principal.subject),
        )

    @app.post(
        "/v1/mode/heightened",
        response_model=CommandAccepted,
        tags=["control"],
        summary="Set heightened mode for a bounded duration.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_mode_heightened(
        req: SetHeightenedModeRequest,
        principal: ApiPrincipal = Depends(require_route_policy("POST", "/v1/mode/heightened")),
        idempotency_key: str = Depends(_require_idempotency_key),
    ) -> CommandAccepted:
        payload = req.model_dump(mode="json")
        return await _execute_command(
            store=command_store,
            principal=principal,
            idempotency_key=idempotency_key,
            command_type="mode.heightened.set",
            payload=payload,
            action=lambda: control.set_heightened_mode(minutes=req.minutes, reason=req.reason, actor=principal.subject),
            success_event="mode.changed",
        )

    @app.delete(
        "/v1/mode/heightened",
        response_model=CommandAccepted,
        tags=["control"],
        summary="Clear heightened mode.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_mode_heightened_clear(
        req: ClearHeightenedModeRequest,
        principal: ApiPrincipal = Depends(require_route_policy("DELETE", "/v1/mode/heightened")),
        idempotency_key: str = Depends(_require_idempotency_key),
    ) -> CommandAccepted:
        payload = req.model_dump(mode="json")
        return await _execute_command(
            store=command_store,
            principal=principal,
            idempotency_key=idempotency_key,
            command_type="mode.heightened.clear",
            payload=payload,
            action=lambda: control.clear_heightened_mode(reason=req.reason, actor=principal.subject),
            success_event="mode.changed",
        )

    @app.post(
        "/v1/tests/originate",
        response_model=CommandAccepted,
        tags=["origination"],
        summary="Originate a configured RWT or RMT test.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_tests_originate(
        req: OriginateTestRequest,
        principal: ApiPrincipal = Depends(require_route_policy("POST", "/v1/tests/originate")),
        idempotency_key: str = Depends(_require_idempotency_key),
    ) -> CommandAccepted:
        payload = req.model_dump(mode="json")
        return await _execute_command(
            store=command_store,
            principal=principal,
            idempotency_key=idempotency_key,
            command_type="tests.originate",
            payload=payload,
            action=lambda: control.originate_test(event_code=req.event_code, actor=principal.subject),
            success_event="alert.originated",
        )

    @app.post(
        "/v1/uploads/audio",
        response_model=AudioUploadAccepted,
        tags=["origination"],
        summary="Stage a WAV upload for later manual audio origination.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_upload_audio(
        file: UploadFile = File(...),
        principal: ApiPrincipal = Depends(require_route_policy("POST", "/v1/uploads/audio")),
    ) -> AudioUploadAccepted:
        data = await file.read()
        try:
            payload = await control.stage_wav_upload(
                filename=file.filename or "upload.wav",
                content_type=file.content_type or "audio/wav",
                data=data,
                actor=principal.subject,
            )
        except ControlError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc
        return AudioUploadAccepted.model_validate(payload)

    @app.post(
        "/v1/inserts/text",
        response_model=CommandAccepted,
        tags=["inserts"],
        summary="Schedule a bounded text insert into the normal broadcast cycle.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_inserts_text(
        req: CreateTextInsertRequest,
        principal: ApiPrincipal = Depends(require_route_policy("POST", "/v1/inserts/text")),
        idempotency_key: str = Depends(_require_idempotency_key),
    ) -> CommandAccepted:
        payload = req.model_dump(mode="json")
        return await _execute_command(
            store=command_store,
            principal=principal,
            idempotency_key=idempotency_key,
            command_type="inserts.text.create",
            payload=payload,
            action=lambda: control.create_text_insert(req, actor=principal.subject),
            success_event="inserts.changed",
        )

    @app.post(
        "/v1/inserts/audio",
        response_model=CommandAccepted,
        tags=["inserts"],
        summary="Schedule a bounded uploaded-audio insert into the normal broadcast cycle.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_inserts_audio(
        req: CreateAudioInsertRequest,
        principal: ApiPrincipal = Depends(require_route_policy("POST", "/v1/inserts/audio")),
        idempotency_key: str = Depends(_require_idempotency_key),
    ) -> CommandAccepted:
        payload = req.model_dump(mode="json")
        return await _execute_command(
            store=command_store,
            principal=principal,
            idempotency_key=idempotency_key,
            command_type="inserts.audio.create",
            payload=payload,
            action=lambda: control.create_audio_insert(req, actor=principal.subject),
            success_event="inserts.changed",
        )

    @app.get(
        "/v1/inserts",
        response_model=CycleInsertList,
        tags=["inserts"],
        summary="List scheduled broadcast-cycle inserts.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_inserts_list(
        include_inactive: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/inserts")),
    ) -> CycleInsertList:
        return CycleInsertList(
            inserts=[
                CycleInsertSnapshot.model_validate(item)
                for item in await control.list_inserts(include_inactive=include_inactive, limit=limit)
            ]
        )

    @app.get(
        "/v1/inserts/{insert_id}",
        response_model=CycleInsertSnapshot,
        tags=["inserts"],
        summary="Return one scheduled broadcast-cycle insert.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_inserts_get(
        insert_id: str,
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/inserts/{insert_id}")),
    ) -> CycleInsertSnapshot:
        return CycleInsertSnapshot.model_validate(await control.get_insert(insert_id))

    @app.delete(
        "/v1/inserts/{insert_id}",
        response_model=CommandAccepted,
        tags=["inserts"],
        summary="Cancel a scheduled broadcast-cycle insert.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_inserts_cancel(
        insert_id: str,
        principal: ApiPrincipal = Depends(require_route_policy("DELETE", "/v1/inserts/{insert_id}")),
        idempotency_key: str = Depends(_require_idempotency_key),
    ) -> CommandAccepted:
        payload = {"insert_id": insert_id}
        return await _execute_command(
            store=command_store,
            principal=principal,
            idempotency_key=idempotency_key,
            command_type="inserts.cancel",
            payload=payload,
            action=lambda: control.cancel_insert(insert_id, actor=principal.subject),
            success_event="inserts.changed",
        )

    @app.post(
        "/v1/originate/text",
        response_model=CommandAccepted,
        tags=["origination"],
        summary="Originate a manual text alert.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_originate_text(
        req: OriginateTextRequest,
        principal: ApiPrincipal = Depends(require_route_policy("POST", "/v1/originate/text")),
        idempotency_key: str = Depends(_require_idempotency_key),
    ) -> CommandAccepted:
        payload = req.model_dump(mode="json")
        return await _execute_command(
            store=command_store,
            principal=principal,
            idempotency_key=idempotency_key,
            command_type="originate.text",
            payload=payload,
            action=lambda: control.originate_text(req, actor=principal.subject),
            success_event="alert.originated",
        )

    @app.post(
        "/v1/originate/audio",
        response_model=CommandAccepted,
        tags=["origination"],
        summary="Originate a manual alert from a staged audio asset.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_originate_audio(
        req: OriginateAudioRequest,
        principal: ApiPrincipal = Depends(require_route_policy("POST", "/v1/originate/audio")),
        idempotency_key: str = Depends(_require_idempotency_key),
    ) -> CommandAccepted:
        payload = req.model_dump(mode="json")
        return await _execute_command(
            store=command_store,
            principal=principal,
            idempotency_key=idempotency_key,
            command_type="originate.audio",
            payload=payload,
            action=lambda: control.originate_audio(req, actor=principal.subject),
            success_event="alert.originated",
        )

    @app.post(
        "/v1/config/reload",
        response_model=CommandAccepted,
        status_code=202,
        tags=["configuration"],
        summary="Reload runtime configuration where hot reload is safe.",
        responses=STANDARD_PROBLEM_RESPONSES,
    )
    async def v1_config_reload(
        req: ConfigReloadRequest,
        principal: ApiPrincipal = Depends(require_route_policy("POST", "/v1/config/reload")),
        idempotency_key: str = Depends(_require_idempotency_key),
    ) -> CommandAccepted:
        if reload_service is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "config_reload_unavailable", "message": "Transactional reload is unavailable."},
            )
        if req.acknowledgment is not None and req.acknowledgment.actor != principal.subject:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "warning_acknowledgment_actor_mismatch",
                    "message": "Warning acknowledgment belongs to a different principal.",
                },
            )
        acknowledgment = (
            WarningAcknowledgment(
                actor=req.acknowledgment.actor,
                candidate_sha256=req.acknowledgment.candidate_sha256,
                candidate_identity_sha256=req.acknowledgment.candidate_identity_sha256,
                report_sha256=req.acknowledgment.report_sha256,
                active_generation=req.acknowledgment.active_generation,
                warning_identities=tuple(req.acknowledgment.warning_identities),
                acknowledged_at=req.acknowledgment.acknowledged_at,
                validator_completed_at=req.acknowledgment.validator_completed_at,
                expires_at=req.acknowledgment.expires_at,
                maximum_age_seconds=req.acknowledgment.maximum_age_seconds,
                clock_skew_seconds=req.acknowledgment.clock_skew_seconds,
                schema_version=req.acknowledgment.schema_version,
            )
            if req.acknowledgment is not None
            else None
        )
        record, replayed = await reload_service.admit(
            ReloadRequest(
                actor=principal.subject,
                reason=req.reason,
                dry_run=req.dry_run,
                expected_generation=req.expected_generation,
                safe_point_timeout_seconds=req.safe_point_timeout_seconds,
                acknowledgment=acknowledgment,
                authorization_context={
                    "kind": principal.kind,
                    "scopes": tuple(sorted(principal.scopes)),
                    "client_host": principal.client_host,
                    "client_id": principal.client_id,
                    "token_id": principal.token_id,
                },
            ),
            idempotency_key=idempotency_key,
        )
        return _command_accepted(record, replayed=replayed)

    @app.get(
        "/v1/events",
        tags=["commands"],
        summary="Stream command and control-plane events as Server-Sent Events.",
        responses={
            200: {
                "description": "Server-Sent Event stream.",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            },
            **STANDARD_PROBLEM_RESPONSES,
        },
    )
    async def v1_events(
        principal: ApiPrincipal = Depends(require_route_policy("GET", "/v1/events")),
    ) -> StreamingResponse:
        queue = await command_store.broker.subscribe()

        async def _stream() -> Any:
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    payload = json.dumps(item["data"], separators=(",", ":"), ensure_ascii=False)
                    yield f"event: {item['event']}\n"
                    yield f"data: {payload}\n\n"
            finally:
                await command_store.broker.unsubscribe(queue)

        return StreamingResponse(_stream(), media_type="text/event-stream")

    return app
