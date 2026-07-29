from __future__ import annotations

import datetime as dt
import json

import pytest

from seasonalweather.jobs.contracts import AttemptOutcome
from seasonalweather.jobs.policies import ExecutorClass, FailureCategory, JobType, QueueClass
from seasonalweather.swwp.auth import (
    ControllerVersionSupport,
    negotiate_subprotocol,
    negotiate_versions,
)
from seasonalweather.swwp.codec import ProtocolCodecError, decode, encode
from seasonalweather.swwp.constants import (
    PROTOCOL_VERSION,
    SUBPROTOCOL,
    ProtocolErrorCategory,
    ProtocolLimits,
    ReconcileDisposition,
)
from seasonalweather.swwp.messages import (
    PAYLOAD_TYPES,
    Cancel,
    CancelAcknowledged,
    CapabilityProbe,
    CapabilityReport,
    CapabilityUpdate,
    CapabilityUpdateAck,
    Drain,
    Drained,
    Envelope,
    Heartbeat,
    HeartbeatAck,
    JobAccepted,
    JobAssignmentPayload,
    JobFailed,
    JobProgress,
    JobRejected,
    JobResult,
    LeaseRef,
    ProtocolErrorPayload,
    Reconcile,
    ReconcileDecision,
    ReconcileItem,
    ReconcileResult,
    Register,
    Registered,
    RegistrationRejected,
    ResultCommitted,
    SelectedVersions,
    VersionSupport,
)
from tests.support.capabilities import wire_manifest, wire_record

NOW = dt.datetime(2026, 7, 24, 12, tzinfo=dt.UTC)
LEASE = LeaseRef(
    job_id="job_00000001",
    lease_id="lease_00000001",
    attempt_id="attempt_00000001",
    attempt=1,
)
MANIFEST = wire_manifest(
    (
        wire_record(
            "tts.synthesis.v1",
            now=NOW,
            parameters={"format": "wav"},
        ),
    )
)
VERSIONS = VersionSupport(
    swwp=(1,),
    job_payloads={JobType.TTS_SYNTHESIZE: (1,)},
    job_results={JobType.TTS_SYNTHESIZE: (1,)},
    diagnostics=(1,),
    capability_manifest=(1,),
    configuration_schema=(1,),
)
SELECTED = SelectedVersions(
    swwp=1,
    job_payloads={JobType.TTS_SYNTHESIZE: 1},
    job_results={JobType.TTS_SYNTHESIZE: 1},
    diagnostics=1,
    capability_manifest=1,
    configuration_schema=1,
)


def registration() -> Register:
    return Register(
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        worker_epoch=1,
        software_version="0.17.0",
        build_identity="build_eaaf24b",
        requested_queues=(QueueClass.ROUTINE,),
        requested_slots=2,
        capability_manifest=MANIFEST,
        supported_versions=VERSIONS,
    )


def _payloads():
    assignment = JobAssignmentPayload(
        lease=LEASE,
        deadline_at=NOW + dt.timedelta(minutes=2),
        lease_expires_at=NOW + dt.timedelta(minutes=1),
        acknowledgment_deadline_at=NOW + dt.timedelta(seconds=10),
        job_type=JobType.TTS_SYNTHESIZE,
        queue=QueueClass.ROUTINE,
        executor=ExecutorClass.ROUTINE_WORKER,
        payload_schema_version=1,
        result_schema_version=1,
        configuration_generation=7,
        payload={
            "content_ref": "content_00000001",
            "voice_profile_ref": "profile_00000001",
            "output_format": "wav",
            "config_generation": 7,
        },
        capability_requirements=("tts.synthesis.v1",),
    )
    item = ReconcileItem(lease=LEASE, prior_session_id="session_00000001", accepted=True)
    return (
        registration(),
        Registered(
            session_id="session_00000001",
            controller_epoch=1,
            selected_subprotocol=SUBPROTOCOL,
            heartbeat_interval_seconds=15,
            heartbeat_timeout_seconds=45,
            lease_seconds=60,
            assignment_ack_seconds=10,
            accepted_queues=(QueueClass.ROUTINE,),
            authorized_job_types=(JobType.TTS_SYNTHESIZE,),
            authorized_capabilities=("tts.synthesis.v1",),
            selected_versions=SELECTED,
            max_message_bytes=65536,
            max_active_assignments=2,
            effective_capabilities=("tts.synthesis.v1",),
            capability_epoch=MANIFEST.epoch,
            capability_digest=MANIFEST.digest,
        ),
        RegistrationRejected(
            category=ProtocolErrorCategory.UNSUPPORTED_VERSION,
            summary="incompatible",
            supported_subprotocols=(SUBPROTOCOL,),
            supported_swwp_versions=(1,),
        ),
        Heartbeat(
            active_leases=(LEASE,),
            capability_epoch=1,
            capability_digest=MANIFEST.digest,
        ),
        HeartbeatAck(renewed=(LEASE,)),
        CapabilityUpdate(
            epoch=2,
            changed=MANIFEST.records,
            full_digest=MANIFEST.digest,
            validity_seconds=60,
        ),
        CapabilityUpdateAck(epoch=2, digest=MANIFEST.digest),
        CapabilityProbe(
            probe_id="probe_00000001",
            full=False,
            names=("tts.synthesis.v1",),
            reason="internal",
            deadline_at=NOW + dt.timedelta(seconds=15),
        ),
        CapabilityReport(
            probe_id="probe_00000001",
            schema_version=1,
            epoch=2,
            records=MANIFEST.records,
            full_digest=MANIFEST.digest,
            validity_seconds=60,
        ),
        assignment,
        JobAccepted(lease=LEASE),
        JobRejected(lease=LEASE, category="capacity_unavailable", summary="busy"),
        JobProgress(lease=LEASE, stage="rendering", numeric={"percent": 50}),
        JobResult(
            lease=LEASE,
            result_schema_version=1,
            result={
                "artifact_ref": "artifact_00000001",
                "content_identity": "content_00000001",
                "duration_seconds": 3.5,
            },
            artifact_refs=("artifact_00000001",),
            completion_id="completion_00000001",
        ),
        JobFailed(
            lease=LEASE,
            outcome=AttemptOutcome.RETRYABLE_FAILURE,
            category=FailureCategory.TRANSIENT_TRANSPORT,
            error_code="transport_failed",
            summary="bounded failure",
        ),
        Cancel(
            lease=LEASE,
            reason="operator_request",
            deadline_at=NOW + dt.timedelta(seconds=5),
        ),
        CancelAcknowledged(lease=LEASE, observed_at=NOW),
        Drain(deadline_at=NOW + dt.timedelta(seconds=30), reason="service_shutdown"),
        Drained(active=(LEASE,), unacknowledged_completions=("completion_00000001",)),
        Reconcile(
            prior_session_id="session_00000001",
            prior_controller_epoch=1,
            items=(item,),
        ),
        ReconcileResult(
            decisions=(
                ReconcileDecision(
                    lease=LEASE,
                    disposition=ReconcileDisposition.RESUME,
                    summary="durable attempt matches",
                ),
            )
        ),
        ResultCommitted(
            lease=LEASE,
            completion_id="completion_00000001",
            result_hash="0123456789abcdef",
            committed_at=NOW,
        ),
        ProtocolErrorPayload(
            category=ProtocolErrorCategory.STATE_VIOLATION,
            summary="invalid state",
            correlated_message_id="message_00000001",
            fatal=True,
        ),
    )


def _envelope(payload, index: int = 1) -> Envelope:
    return Envelope(
        message_type=payload.message_type,
        message_id=f"message_{index:08d}",
        sent_at=NOW,
        session_id=None if isinstance(payload, Register) else "session_00000001",
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        controller_epoch=None if isinstance(payload, Register) else 1,
        worker_epoch=1,
        payload=payload,
    )


@pytest.mark.parametrize("payload", _payloads(), ids=lambda item: item.message_type)
def test_codec_round_trip_for_every_message(payload) -> None:
    envelope = _envelope(payload)

    encoded = encode(envelope)

    assert decode(encoded) == envelope
    assert encoded == encode(decode(encoded))


def test_message_registry_and_canonical_json_cover_complete_vocabulary() -> None:
    assert {model.message_type for model in PAYLOAD_TYPES} == {
        "register",
        "registered",
        "registration_rejected",
        "heartbeat",
        "heartbeat_ack",
        "capability_update",
        "capability_update_ack",
        "capability_probe",
        "capability_report",
        "job",
        "job_accepted",
        "job_rejected",
        "job_progress",
        "job_result",
        "job_failed",
        "cancel",
        "cancel_acknowledged",
        "drain",
        "drained",
        "reconcile",
        "reconcile_result",
        "result_committed",
        "protocol_error",
        "diagnostic",
        "diagnostic_ack",
    }
    encoded = encode(_envelope(registration()))
    assert (
        encoded
        == json.dumps(
            json.loads(encoded),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


@pytest.mark.parametrize(
    ("raw", "category"),
    (
        (b"{", ProtocolErrorCategory.MALFORMED_JSON),
        (b'{"x":1,"x":2}', ProtocolErrorCategory.MALFORMED_JSON),
        (b"[]", ProtocolErrorCategory.INVALID_ENVELOPE),
        (b'{"message_type":"future"}', ProtocolErrorCategory.UNKNOWN_MESSAGE_TYPE),
        (
            b'{"protocol_version":2,"message_type":"heartbeat","payload":{}}',
            ProtocolErrorCategory.UNSUPPORTED_VERSION,
        ),
        (
            b'{"protocol_version":1,"message_type":"heartbeat","payload":{"unknown":1}}',
            ProtocolErrorCategory.INVALID_PAYLOAD,
        ),
        (
            b'{"protocol_version":1,"message_type":"heartbeat","payload":{"active_leases":NaN}}',
            ProtocolErrorCategory.MALFORMED_JSON,
        ),
    ),
)
def test_codec_rejects_malformed_unknown_and_invalid_input(raw: bytes, category) -> None:
    with pytest.raises(ProtocolCodecError) as raised:
        decode(raw)
    assert raised.value.category is category


def test_codec_bounds_encoded_bytes_depth_and_collections() -> None:
    encoded = encode(_envelope(registration()))
    with pytest.raises(ProtocolCodecError, match="limit") as raised:
        decode(encoded, limits=ProtocolLimits(max_message_bytes=100))
    assert raised.value.category is ProtocolErrorCategory.OVERSIZED

    raw = b'{"protocol_version":1,"message_type":"heartbeat","payload":{"active_leases":[[[[[]]]]]}}'
    with pytest.raises(ProtocolCodecError) as raised:
        decode(raw, limits=ProtocolLimits(max_depth=3))
    assert raised.value.category is ProtocolErrorCategory.OVERSIZED


def test_exact_subprotocol_and_independent_version_negotiation() -> None:
    assert negotiate_subprotocol((SUBPROTOCOL,)) == SUBPROTOCOL
    with pytest.raises(ValueError):
        negotiate_subprotocol(("other", SUBPROTOCOL))

    selected = negotiate_versions(registration(), ControllerVersionSupport())
    assert selected == SELECTED
    incompatible = registration().model_copy(
        update={"supported_versions": VERSIONS.model_copy(update={"diagnostics": (2,)})}
    )
    with pytest.raises(ValueError, match="diagnostic"):
        negotiate_versions(incompatible, ControllerVersionSupport())


def test_unknown_fields_naive_timestamps_and_non_json_types_fail_closed() -> None:
    with pytest.raises(ValueError):
        Register.model_validate(registration().model_dump() | {"unexpected": True})
    with pytest.raises(ValueError, match="timezone"):
        Envelope(
            protocol_version=PROTOCOL_VERSION,
            message_type="register",
            message_id="message_00000001",
            sent_at=dt.datetime(2026, 7, 24, 12),
            payload=registration(),
        )
    envelope = _envelope(registration())
    with pytest.raises(ProtocolCodecError):
        encode(envelope.model_copy(update={"payload": object()}))
