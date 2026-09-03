"""Shared request validation without controller qualification or publication state."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from seasonalweather.validation.admission import AdmissionRejection, admission_error, tts_field

from .failures import ProcessFailure
from .models import MAX_SYNTHESIS_TEXT, BackendId, SynthesisRequest

if TYPE_CHECKING:
    from .local import LocalEngineRegistry


def _local_engine_registry() -> type[LocalEngineRegistry]:
    from .local import LocalEngineRegistry

    return LocalEngineRegistry


def _validate_tts_shape(request: SynthesisRequest, now: dt.datetime | None) -> AdmissionRejection | None:
    if request.deadline_at <= (now or dt.datetime.now(dt.UTC)):
        return _rejection("invalid_deadline", ("deadline_at",), "TTS synthesis deadline has already expired.")
    if request.output.format != "wav":
        return _rejection(
            "unsupported_output_format", ("output", "format"), "TTS synthesis currently admits WAV output only."
        )
    if len(request.text.encode("utf-8")) > MAX_SYNTHESIS_TEXT:
        return _rejection("input_limit", ("text",), "TTS input exceeds its bounded UTF-8 size limit.")
    return None


def _validate_tts_fallback(request: SynthesisRequest, fallback_viability: bool | None) -> AdmissionRejection | None:
    if request.backend is BackendId.LOCAL and request.fallback_backend is not None:
        return _rejection(
            "unsupported_fallback_direction",
            ("fallback_backend",),
            "Local synthesis cannot select a remote fallback.",
        )
    if fallback_viability is False:
        return _rejection(
            "fallback_unavailable", ("fallback_backend",), "The configured TTS fallback is not currently viable."
        )
    if request.backend is not BackendId.LOCAL and request.fallback_backend not in {None, BackendId.LOCAL}:
        return _rejection(
            "unsupported_fallback",
            ("fallback_backend",),
            "The current fallback policy permits only remote primary to local fallback.",
        )
    return None


def _validate_local_tts_profile(request: SynthesisRequest) -> AdmissionRejection | None:
    if request.backend is not BackendId.LOCAL and request.fallback_backend is not BackendId.LOCAL:
        return None
    try:
        engine = _local_engine_registry().normalize(request.local.engine)
        _local_engine_registry().validate_voice(engine, request.local.voice)
    except (ValueError, ProcessFailure):
        return _rejection(
            "unsupported_engine",
            ("local", "engine"),
            "The local engine or voice profile is not supported.",
        )
    return None


def _validate_tts_qualification(request: SynthesisRequest, qualification: object | None) -> AdmissionRejection | None:
    if qualification is None or (request.backend is not BackendId.LOCAL and request.fallback_backend is not None):
        return None
    raw_disposition = getattr(qualification, "disposition", None)
    disposition = str(getattr(raw_disposition, "value", raw_disposition) or "")
    qualified = getattr(qualification, "qualified", None)
    capacity = getattr(qualification, "effective_capacity", 1)
    if (disposition in {"satisfied", "degraded", "degraded_fallback"} or qualified is True) and capacity > 0:
        return None
    return _rejection(
        "capability_unavailable",
        ("backend",),
        "The current P1-09 capability qualification cannot admit TTS synthesis.",
    )


def validate_synthesis_request(
    request: SynthesisRequest,
    *,
    qualification: object | None = None,
    now: dt.datetime | None = None,
    fallback_viability: bool | None = None,
) -> AdmissionRejection | None:
    """Return one bounded typed diagnostic before engine execution.

    ``qualification`` is evidence supplied by P1-09; this function only
    translates it into P1-14 admission diagnostics and never owns its state.
    """

    for check in (
        _validate_tts_shape(request, now),
        _validate_tts_fallback(request, fallback_viability),
        _validate_local_tts_profile(request),
        _validate_tts_qualification(request, qualification),
    ):
        if check is not None:
            return check
    return None


def _rejection(reason: str, field: tuple[str, ...], message: str) -> AdmissionRejection:
    return AdmissionRejection(
        reason_code=reason,
        message=message,
        issue=admission_error(
            tts_field(*field),
            message=message,
            help_text="Correct the bounded TTS request and select a supported local profile.",
        ),
    )
