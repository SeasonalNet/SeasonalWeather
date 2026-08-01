"""Reusable bounded admission diagnostics without future subsystem ownership."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from seasonalweather.diagnostics.models import DiagnosticSeverity

from .issues import ValidationIssue, ValidationStage
from .paths import DiagnosticPath, PathKind, PathSegment


@dataclass(frozen=True)
class AdmissionField:
    kind: PathKind
    segments: tuple[PathSegment, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "segments", tuple(self.segments))

    def path(self) -> DiagnosticPath:
        return DiagnosticPath(self.kind, self.segments)


@dataclass(frozen=True)
class AdmissionRejection:
    """Typed rejection that an owning boundary can translate without mutation."""

    reason_code: str
    message: str
    issue: ValidationIssue
    status_code: int = 422
    maximum_bytes: int | None = None


def admission_error(
    field: AdmissionField,
    *,
    message: str,
    help_text: str,
    notes: tuple[str, ...] = (),
) -> ValidationIssue:
    """Build one value-free admission issue from an owning validator's result."""

    return ValidationIssue(
        rule_id="admission.invalid",
        validator_rule_id="admission.field",
        phase=ValidationStage.SEMANTIC,
        severity=DiagnosticSeverity.ERROR,
        blocking=True,
        message=message,
        path=field.path(),
        notes=notes,
        help=help_text,
        operational_effect="The bounded input is rejected before it can affect broadcast state.",
        documentation_reference="docs/configuration-validation.md",
    )


def job_payload_field(*segments: PathSegment) -> AdmissionField:
    return AdmissionField(PathKind.JOB_PAYLOAD, tuple(segments))


def authentication_field(*segments: PathSegment) -> AdmissionField:
    return AdmissionField(PathKind.AUTHENTICATION, tuple(segments))


def upload_field(*segments: PathSegment) -> AdmissionField:
    return AdmissionField(PathKind.UPLOAD, tuple(segments))


def scheduled_insert_field(*segments: PathSegment) -> AdmissionField:
    return AdmissionField(PathKind.SCHEDULED_INSERT, tuple(segments))


def tts_field(*segments: PathSegment) -> AdmissionField:
    return AdmissionField(PathKind.TTS, tuple(segments))


def segment_field(*segments: PathSegment) -> AdmissionField:
    return AdmissionField(PathKind.SEGMENT, tuple(segments))


def import_feature_field(source: str, feature: str, *segments: PathSegment) -> AdmissionField:
    return AdmissionField(PathKind.IMPORT, (source, feature, *segments))


def json_payload_field(pointer: str) -> AdmissionField:
    path = DiagnosticPath.json_pointer(pointer)
    return AdmissionField(PathKind.JSON_POINTER, path.segments)


def validate_wav_upload(
    *,
    filename: str,
    content_type: str,
    size_bytes: int,
    maximum_bytes: int,
) -> AdmissionRejection | None:
    """Validate the bounded metadata available before an upload is persisted."""

    if size_bytes == 0:
        return AdmissionRejection(
            reason_code="empty_upload",
            message="Uploaded audio file was empty.",
            issue=admission_error(
                upload_field("data"),
                message="Uploaded audio file was empty.",
                help_text="Upload a non-empty WAV file.",
            ),
        )
    if size_bytes > maximum_bytes:
        return AdmissionRejection(
            reason_code="upload_too_large",
            message="Uploaded audio exceeds the configured size limit.",
            status_code=413,
            maximum_bytes=maximum_bytes,
            issue=admission_error(
                upload_field("size_bytes"),
                message="Uploaded audio exceeds the configured size limit.",
                help_text="Upload a WAV file within the configured byte limit.",
            ),
        )

    filename_clean = Path(filename or "upload.wav").name or "upload.wav"
    if Path(filename_clean).suffix.lower() != ".wav":
        return AdmissionRejection(
            reason_code="unsupported_audio_type",
            message="Only .wav uploads are supported in v1.",
            issue=admission_error(
                upload_field("filename"),
                message="The upload filename does not use the .wav extension.",
                help_text="Use a filename ending in .wav.",
            ),
        )
    allowed_content_types = {
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "application/octet-stream",
    }
    if content_type and content_type.lower() not in allowed_content_types:
        return AdmissionRejection(
            reason_code="unsupported_audio_type",
            message="Only WAV uploads are supported in v1.",
            issue=admission_error(
                upload_field("content_type"),
                message="The upload content type is not supported for WAV audio.",
                help_text="Use a supported WAV content type.",
            ),
        )
    return None
