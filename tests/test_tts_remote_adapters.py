from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import ssl
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import httpx
import pytest

from seasonalweather.tts.adapters import (
    OpenAICompatibleAdapter,
    OpenAICompatibleConfig,
    SeasonalTtsdAdapter,
    SeasonalTtsdConfig,
)
from seasonalweather.tts.adapters.models import ProviderAudio, _AccessToken
from seasonalweather.tts.failures import ProcessFailure
from seasonalweather.tts.models import (
    AcceptedArtifactReference,
    ArtifactEvidence,
    BackendId,
    SynthesisFailure,
    SynthesisPurpose,
    SynthesisRequest,
)
from seasonalweather.tts.service import SynthesisService
from seasonalweather.tts.tts import TTS

CLIENT = "seasonalttsd_client_" + "c" * 43
ACCESS = "seasonalttsd_access_" + "a" * 43
TEXT = "plain normalized text"


class FakeResponse:
    def __init__(self, status_code: int, body: bytes, content_type: str = "application/json") -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type, "content-length": str(len(body))}
        self.body = body
        self.closed = False

    async def aiter_bytes(self, chunk_size: int = 65_536):
        del chunk_size
        yield self.body

    async def aclose(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []
        self.lock = threading.Lock()

    async def request(self, method, url, *, headers, json, timeout):
        with self.lock:
            self.requests.append(
                {"method": method, "url": url, "headers": dict(headers), "json": json, "timeout": timeout}
            )
            selected = self.responses.pop(0)
        if isinstance(selected, BaseException):
            raise selected
        return selected

    async def close(self) -> None:
        pass


class BlockingTransport:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.entered = threading.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.requests = 0

    async def request(self, method, url, *, headers, json, timeout):
        del method, url, headers, json, timeout
        self.requests += 1
        self.entered.set()
        await self.release.wait()
        return self.response

    async def close(self) -> None:
        self.closed = True
        self.release.set()


class BlockingBodyResponse(FakeResponse):
    def __init__(self) -> None:
        super().__init__(200, b"first", "audio/wav")
        self.body_started = threading.Event()
        self.release = asyncio.Event()

    async def aiter_bytes(self, chunk_size: int = 65_536):
        del chunk_size
        yield self.body
        self.body_started.set()
        await self.release.wait()
        yield b"second"

    async def aclose(self) -> None:
        self.release.set()
        await super().aclose()


class BlockingErrorBodyResponse(BlockingBodyResponse):
    def __init__(self, status_code: int = 403) -> None:
        super().__init__()
        self.status_code = status_code
        self.headers["content-type"] = "text/plain"


class BlockingCloseResponse(FakeResponse):
    def __init__(self, status_code: int = 200, body: bytes = b"body", content_type: str = "audio/wav") -> None:
        super().__init__(status_code, body, content_type)
        self.close_started = threading.Event()
        self.close_cancelled = threading.Event()
        self._close_release = asyncio.Event()

    async def aclose(self) -> None:
        self.close_started.set()
        try:
            await self._close_release.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise


class BlockingCloseTransport:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.response = response or _audio_response()
        self.request_started = threading.Event()
        self.close_started = threading.Event()
        self.close_cancelled = threading.Event()
        self._release = asyncio.Event()

    async def request(self, method, url, *, headers, json, timeout):
        del method, url, headers, json, timeout
        self.request_started.set()
        await self._release.wait()
        return self.response

    async def close(self) -> None:
        self.close_started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise


class CompositeCleanupTransport(BlockingCloseTransport):
    def __init__(self, response: FakeResponse) -> None:
        super().__init__(response)

    async def request(self, method, url, *, headers, json, timeout):
        del method, url, headers, json, timeout
        self.request_started.set()
        return self.response


class CompositeBlockingBodyResponse(BlockingCloseResponse):
    def __init__(self, status_code: int = 403) -> None:
        super().__init__(status_code=status_code, body=b"first", content_type="text/plain")
        self.body_started = threading.Event()

    async def aiter_bytes(self, chunk_size: int = 65_536):
        del chunk_size
        yield self.body
        self.body_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise


class OperationTransportFactory:
    def __init__(self, transports: list[BlockingCloseTransport | FakeTransport]) -> None:
        self.transports = transports
        self.created: list[BlockingCloseTransport | FakeTransport] = []

    def for_operation(self):
        transport = self.transports[len(self.created)]
        self.created.append(transport)
        return transport


class ProgressingResponse(FakeResponse):
    def __init__(self, *, delay: float = 0.01) -> None:
        super().__init__(200, b"", "audio/wav")
        self.delay = delay

    async def aiter_bytes(self, chunk_size: int = 65_536):
        del chunk_size
        chunk = _valid_wav()
        while True:
            await asyncio.sleep(self.delay)
            yield chunk


class DelayedTransport(FakeTransport):
    def __init__(self, delay: float, responses: list[FakeResponse | BaseException]) -> None:
        super().__init__(responses)
        self.delay = delay

    async def request(self, method, url, *, headers, json, timeout):
        await asyncio.sleep(self.delay)
        return await super().request(method, url, headers=headers, json=json, timeout=timeout)


class SeasonalPhaseTransport:
    def __init__(self, *, block_synthesis: bool = False) -> None:
        self.block_synthesis = block_synthesis
        self.requests: list[dict[str, object]] = []
        self.entered = threading.Event()
        self.release = asyncio.Event()

    async def request(self, method, url, *, headers, json, timeout):
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "json": json, "timeout": timeout})
        if url.endswith("/v1/auth/token"):
            return _token_response()
        if self.block_synthesis:
            self.entered.set()
            await self.release.wait()
        return _audio_response()

    async def close(self) -> None:
        self.release.set()


class DelayedSeasonal401Transport:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self._synthesis_attempts = 0

    async def request(self, method, url, *, headers, json, timeout):
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "json": json, "timeout": timeout})
        if url.endswith("/v1/auth/token"):
            return _token_response()
        self._synthesis_attempts += 1
        if self._synthesis_attempts == 1:
            await asyncio.sleep(0.1)
            return FakeResponse(401, b"expired")
        return _audio_response()

    async def close(self) -> None:
        pass


class BlockingSeasonalRefreshTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.token_requests = 0
        self.synthesis_requests = 0
        self.refresh_started = threading.Event()
        self._release = asyncio.Event()

    async def request(self, method, url, *, headers, json, timeout):
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "json": json, "timeout": timeout})
        if url.endswith("/v1/auth/token"):
            self.token_requests += 1
            if self.token_requests == 2:
                self.refresh_started.set()
                await self._release.wait()
            return _token_response()
        self.synthesis_requests += 1
        return FakeResponse(401, b"expired")

    async def close(self) -> None:
        self._release.set()


def _request(backend: BackendId, *, fallback: BackendId | None = None) -> SynthesisRequest:
    return SynthesisRequest(
        purpose=SynthesisPurpose.ROUTINE,
        backend=backend,
        fallback_backend=fallback,
        text=TEXT,
        deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=10),
        configuration_generation=4,
    )


def _token_response(expires_in: int = 900) -> FakeResponse:
    return FakeResponse(
        200,
        json.dumps(
            {
                "access_token": ACCESS,
                "token_type": "Bearer",
                "expires_at": "2030-01-01T00:00:00+00:00",
                "expires_in": expires_in,
                "scopes": ["tts:synthesize"],
                "allowed_prefixes": ["/v1/syntheses"],
            }
        ).encode(),
    )


def _valid_wav() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(48_000)
        writer.writeframes(b"\x00\x00\x00\x00" * 480)
    return stream.getvalue()


def _audio_response(body: bytes | None = None) -> FakeResponse:
    body = _valid_wav() if body is None else body
    return FakeResponse(200, body, "audio/wav")


def _tts_helper_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name in {"seasonalweather-tts-transport", "seasonalweather-tts-body"}
    ]


def test_seasonal_ttsd_is_disabled_without_configuration_and_does_not_contact_provider(tmp_path: Path) -> None:
    adapter = SeasonalTtsdAdapter(SeasonalTtsdConfig(), transport=FakeTransport([]))
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert error.value.classification == "invalid_input"


def test_openai_compatible_is_disabled_without_configuration_and_does_not_contact_provider(tmp_path: Path) -> None:
    transport = FakeTransport([])
    adapter = OpenAICompatibleAdapter(OpenAICompatibleConfig(), transport=transport)
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert error.value.classification == "authentication_failed"
    assert transport.requests == []


def test_seasonal_ttsd_exact_token_exchange_request_and_reuse(tmp_path: Path) -> None:
    credential = tmp_path / "client.credential"
    credential.write_text(CLIENT, encoding="ascii")
    transport = FakeTransport([_token_response(), _audio_response(), _audio_response()])
    adapter = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(base_url="https://tts.example.test", client_credential_file=str(credential)),
        transport=transport,
    )
    for index in range(2):
        result = adapter.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path / str(index),
            deadline=9999999999,
            cancellation=threading.Event(),
        )
        assert result.path.exists()
    assert len(transport.requests) == 3
    assert transport.requests[0]["url"] == "https://tts.example.test/v1/auth/token"
    assert transport.requests[0]["json"] == {
        "requested_scopes": ["tts:synthesize"],
        "requested_prefixes": ["/v1/syntheses"],
        "ttl_seconds": 900,
    }
    assert transport.requests[1]["url"] == "https://tts.example.test/v1/syntheses"
    assert transport.requests[1]["json"] == {
        "input": {"type": "text", "content": TEXT},
        "voice": "voicetext-paul",
        "profile": "wav-48k-stereo",
    }
    assert transport.requests[1]["headers"]["Authorization"] == f"Bearer {ACCESS}"


def test_seasonal_ttsd_refresh_is_serialized_and_401_retries_once(tmp_path: Path) -> None:
    credential = tmp_path / "client.credential"
    credential.write_text(CLIENT, encoding="ascii")
    transport = FakeTransport(
        [_token_response(), FakeResponse(401, b'{"error":"secret-body"}'), _token_response(), _audio_response()]
    )
    adapter = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(base_url="https://tts.example.test", client_credential_file=str(credential)),
        transport=transport,
    )
    result = adapter.synthesize(
        _request(BackendId.SEASONAL_TTSD),
        TEXT,
        output_dir=tmp_path,
        deadline=9999999999,
        cancellation=threading.Event(),
    )
    assert result.path.exists()
    assert [request["url"] for request in transport.requests] == [
        "https://tts.example.test/v1/auth/token",
        "https://tts.example.test/v1/syntheses",
        "https://tts.example.test/v1/auth/token",
        "https://tts.example.test/v1/syntheses",
    ]


def test_seasonal_ttsd_concurrent_refresh_uses_one_token_exchange(tmp_path: Path) -> None:
    credential = tmp_path / "client.credential"
    credential.write_text(CLIENT, encoding="ascii")
    transport = FakeTransport([_token_response(), _audio_response(), _audio_response()])
    adapter = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(base_url="https://tts.example.test", client_credential_file=str(credential)),
        transport=transport,
    )

    def synthesize(index: int):
        return adapter.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path / str(index),
            deadline=9999999999,
            cancellation=threading.Event(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(synthesize, range(2)))
    assert all(result.path.exists() for result in results)
    assert sum(request["url"].endswith("/v1/auth/token") for request in transport.requests) == 1


def test_seasonal_ttsd_refreshes_token_inside_margin(tmp_path: Path) -> None:
    credential = tmp_path / "client.credential"
    credential.write_text(CLIENT, encoding="ascii")
    transport = FakeTransport([_token_response(1), _audio_response(), _token_response(), _audio_response()])
    adapter = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(
            base_url="https://tts.example.test",
            client_credential_file=str(credential),
            refresh_margin_seconds=120,
        ),
        transport=transport,
    )
    for index in range(2):
        adapter.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path / str(index),
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert sum(request["url"].endswith("/v1/auth/token") for request in transport.requests) == 2


def test_seasonal_ttsd_403_does_not_refresh(tmp_path: Path) -> None:
    credential = tmp_path / "client.credential"
    credential.write_text(CLIENT, encoding="ascii")
    transport = FakeTransport([_token_response(), FakeResponse(403, b"denied")])
    adapter = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(base_url="https://tts.example.test", client_credential_file=str(credential)),
        transport=transport,
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert error.value.classification == "authorization_failed"
    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    ("status", "classification"),
    [(429, "rate_limited"), (500, "provider_failed"), (302, "redirect_rejected")],
)
def test_remote_statuses_are_bounded_typed_failures(tmp_path: Path, status: int, classification: str) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    transport = FakeTransport([FakeResponse(status, f"raw provider secret detail {TEXT} sk-test-secret".encode())])
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=transport,
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert error.value.classification == classification
    assert "raw provider secret" not in str(error.value)
    assert TEXT not in str(error.value)
    assert "sk-test-secret" not in repr(adapter)


def test_seasonal_401_refreshes_even_when_error_body_is_oversized_or_malformed(tmp_path: Path) -> None:
    credential = tmp_path / "client.credential"
    credential.write_text(CLIENT, encoding="ascii")
    for body in (b"x" * 128, b"not-json"):
        transport = FakeTransport([_token_response(), FakeResponse(401, body), _token_response(), _audio_response()])
        adapter = SeasonalTtsdAdapter(
            SeasonalTtsdConfig(
                base_url="https://tts.example.test",
                client_credential_file=str(credential),
                max_error_bytes=4,
            ),
            transport=transport,
        )
        result = adapter.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path / str(len(body)),
            deadline=9999999999,
            cancellation=threading.Event(),
        )
        assert result.path.exists()
        assert sum(request["url"].endswith("/v1/auth/token") for request in transport.requests) == 2


@pytest.mark.parametrize(
    ("status", "classification"),
    [(403, "authorization_failed"), (429, "rate_limited"), (504, "provider_timed_out")],
)
def test_http_status_classification_wins_over_oversized_error_body(
    tmp_path: Path, status: int, classification: str
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="m",
            voice="v",
            max_error_bytes=4,
        ),
        transport=FakeTransport([FakeResponse(status, b"x" * 128)]),
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert error.value.classification == classification


@pytest.mark.parametrize("status", [403, 429, 502])
def test_provider_deadline_preempts_blocking_tolerated_error_body(tmp_path: Path, status: int) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    response = BlockingErrorBodyResponse(status)
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="m",
            voice="v",
            synthesis_timeout_seconds=0.05,
        ),
        transport=FakeTransport([response]),
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert error.value.classification == "provider_timed_out"
    assert response.closed


def test_seasonal_401_body_timeout_performs_no_refresh_or_retry(tmp_path: Path) -> None:
    credential = tmp_path / "client"
    credential.write_text(CLIENT, encoding="ascii")
    response = BlockingErrorBodyResponse(401)
    transport = FakeTransport([_token_response(), response, _token_response(), _audio_response()])
    adapter = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(
            base_url="https://tts.example.test",
            client_credential_file=str(credential),
            synthesis_timeout_seconds=0.05,
        ),
        transport=transport,
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert error.value.classification == "provider_timed_out"
    assert [str(call["url"]) for call in transport.requests] == [
        "https://tts.example.test/v1/auth/token",
        "https://tts.example.test/v1/syntheses",
    ]


def test_seasonal_refresh_cannot_outlive_original_synthesis_fence(tmp_path: Path) -> None:
    credential = tmp_path / "client"
    credential.write_text(CLIENT, encoding="ascii")
    transport = BlockingSeasonalRefreshTransport()
    adapter = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(
            base_url="https://tts.example.test",
            client_credential_file=str(credential),
            token_timeout_seconds=1,
            synthesis_timeout_seconds=0.05,
        ),
        transport=transport,
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert error.value.classification == "provider_timed_out"
    assert transport.refresh_started.is_set()
    assert transport.token_requests == 2
    assert transport.synthesis_requests == 1


def test_openai_compatible_exact_request_and_file_authentication(tmp_path: Path) -> None:
    key = tmp_path / "api-key"
    key.write_text("sk-test-secret", encoding="ascii")
    transport = FakeTransport([_audio_response()])
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1/",
            api_key_file=str(key),
            model="configured-model",
            voice="configured-voice",
            response_format="wav",
            speed=0.85,
        ),
        transport=transport,
    )
    result = adapter.synthesize(
        _request(BackendId.OPENAI_COMPATIBLE),
        TEXT,
        output_dir=tmp_path / "audio",
        deadline=9999999999,
        cancellation=threading.Event(),
    )
    assert result.format == "wav"
    call = transport.requests[0]
    assert call["url"] == "https://api.example.test/v1/audio/speech"
    assert call["headers"]["Authorization"] == "Bearer sk-test-secret"
    assert call["json"] == {
        "model": "configured-model",
        "voice": "configured-voice",
        "input": TEXT,
        "response_format": "wav",
        "speed": 0.85,
    }


def test_credential_bounds_and_transport_tls_failure_are_redacted(tmp_path: Path) -> None:
    credential = tmp_path / "credential"
    credential.write_text("x" * 513, encoding="ascii")
    adapter = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(base_url="https://tts.example.test", client_credential_file=str(credential)),
        transport=FakeTransport([]),
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert error.value.classification == "authentication_failed"
    assert "x" * 50 not in str(error.value)

    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    tls = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=FakeTransport([httpx.ConnectError("private detail")]),
    )
    with pytest.raises(ProcessFailure) as tls_error:
        tls.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert tls_error.value.classification == "transport_failed"
    assert "private detail" not in str(tls_error.value)

    tls = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=FakeTransport([ssl.SSLError("certificate detail")]),
    )
    with pytest.raises(ProcessFailure) as validation_error:
        tls.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert validation_error.value.classification == "tls_failed"
    assert "certificate detail" not in str(validation_error.value)


@pytest.mark.parametrize(
    ("response", "classification"),
    [
        (FakeResponse(200, b"no", "application/json"), "unsupported_audio_format"),
        (FakeResponse(200, b"four", "audio/wav"), "response_too_large"),
    ],
)
def test_openai_response_media_and_size_are_validated(
    tmp_path: Path, response: FakeResponse, classification: str
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="m",
            voice="v",
            max_response_bytes=3,
        ),
        transport=FakeTransport([response]),
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert error.value.classification == classification


def test_remote_timeout_and_malformed_token_response_are_classified(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    timeout_adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=FakeTransport([httpx.ReadTimeout("provider detail")]),
    )
    with pytest.raises(ProcessFailure) as timeout_error:
        timeout_adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert timeout_error.value.classification == "provider_timed_out"

    credential = tmp_path / "client"
    credential.write_text(CLIENT, encoding="ascii")
    malformed = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(base_url="https://tts.example.test", client_credential_file=str(credential)),
        transport=FakeTransport([FakeResponse(200, b"{}")]),
    )
    with pytest.raises(ProcessFailure) as malformed_error:
        malformed.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert malformed_error.value.classification == "malformed_response"


def test_remote_input_response_media_and_cancellation_limits(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="m",
            voice="v",
            max_input_bytes=3,
            max_response_bytes=3,
        ),
        transport=FakeTransport([]),
    )
    with pytest.raises(ProcessFailure, match="input") as input_error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert input_error.value.classification == "input_limit"

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ProcessFailure) as cancel_error:
        OpenAICompatibleAdapter(
            OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
            transport=FakeTransport([_audio_response()]),
        ).synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=cancelled,
        )
    assert cancel_error.value.classification == "cancelled"


def test_remote_deadline_and_cancellation_fences_prevent_provider_calls(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    expired_transport = FakeTransport([])
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=expired_transport,
    )
    with pytest.raises(ProcessFailure) as expired:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() - 1,
            cancellation=threading.Event(),
        )
    assert expired.value.classification == "timed_out"
    assert expired_transport.requests == []

    cancelled_transport = FakeTransport([])
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ProcessFailure) as cancelled_error:
        OpenAICompatibleAdapter(
            OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
            transport=cancelled_transport,
        ).synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() + 1,
            cancellation=cancelled,
        )
    assert cancelled_error.value.classification == "cancelled"
    assert cancelled_transport.requests == []


def test_remote_header_and_streaming_body_fences_are_bounded(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    header_transport = BlockingTransport(_audio_response())
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=header_transport,
    )
    with pytest.raises(ProcessFailure) as header_error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() + 0.05,
            cancellation=threading.Event(),
        )
    assert header_error.value.classification == "timed_out"
    assert not header_transport.closed
    assert _tts_helper_threads() == []

    body = BlockingBodyResponse()
    body_transport = FakeTransport([body])
    body_adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=body_transport,
    )
    with pytest.raises(ProcessFailure) as body_error:
        body_adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() + 0.05,
            cancellation=threading.Event(),
        )
    assert body_error.value.classification == "timed_out"
    assert body.closed
    assert _tts_helper_threads() == []


def test_remote_header_and_body_cancellation_join_owned_helpers(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")

    header_transport = BlockingTransport(_audio_response())
    header_cancel = threading.Event()
    header_adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=header_transport,
    )
    header_result: list[ProcessFailure] = []
    header_thread = threading.Thread(
        target=lambda: _capture_failure(
            header_result,
            header_adapter,
            tmp_path / "header-cancel",
            header_cancel,
        )
    )
    header_thread.start()
    assert header_transport.entered.wait(1)
    header_cancel.set()
    header_thread.join(1)
    assert not header_thread.is_alive()
    assert header_result[0].classification == "cancelled"
    assert _tts_helper_threads() == []

    body = BlockingBodyResponse()
    body_cancel = threading.Event()
    body_adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=FakeTransport([body]),
    )
    body_result: list[ProcessFailure] = []
    body_thread = threading.Thread(
        target=lambda: _capture_failure(body_result, body_adapter, tmp_path / "body-cancel", body_cancel)
    )
    body_thread.start()
    assert body.body_started.wait(1)
    body_cancel.set()
    body_thread.join(1)
    assert not body_thread.is_alive()
    assert body_result[0].classification == "cancelled"
    assert body.closed
    assert _tts_helper_threads() == []


def test_provider_timeout_fences_openai_header_and_body_without_global_timeout(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    header_transport = BlockingTransport(_audio_response())
    header_adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="m",
            voice="v",
            synthesis_timeout_seconds=0.05,
        ),
        transport=header_transport,
    )
    with pytest.raises(ProcessFailure) as header_error:
        header_adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path / "header",
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert header_error.value.classification == "provider_timed_out"
    assert _tts_helper_threads() == []

    body = BlockingBodyResponse()
    body_adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="m",
            voice="v",
            synthesis_timeout_seconds=0.05,
        ),
        transport=FakeTransport([body]),
    )
    with pytest.raises(ProcessFailure) as body_error:
        body_adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path / "body",
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert body_error.value.classification == "provider_timed_out"
    assert body.closed
    assert _tts_helper_threads() == []


def test_provider_wait_longer_than_legacy_quarter_second_succeeds(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="m",
            voice="v",
            synthesis_timeout_seconds=1,
        ),
        transport=DelayedTransport(0.3, [_audio_response()]),
    )
    result = adapter.synthesize(
        _request(BackendId.OPENAI_COMPATIBLE),
        TEXT,
        output_dir=tmp_path,
        deadline=time.monotonic() + 2,
        cancellation=threading.Event(),
    )
    assert result.path.exists()


def test_seasonal_token_and_synthesis_subdeadlines_are_independent(tmp_path: Path) -> None:
    credential = tmp_path / "client"
    credential.write_text(CLIENT, encoding="ascii")
    token_transport = BlockingTransport(_token_response())
    token_adapter = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(
            base_url="https://tts.example.test",
            client_credential_file=str(credential),
            token_timeout_seconds=0.05,
        ),
        transport=token_transport,
    )
    with pytest.raises(ProcessFailure) as token_error:
        token_adapter.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path / "token",
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert token_error.value.classification == "provider_timed_out"

    synthesis_transport = SeasonalPhaseTransport(block_synthesis=True)
    synthesis_adapter = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(
            base_url="https://tts.example.test",
            client_credential_file=str(credential),
            token_timeout_seconds=1,
            synthesis_timeout_seconds=0.05,
        ),
        transport=synthesis_transport,
    )
    with pytest.raises(ProcessFailure) as synthesis_error:
        synthesis_adapter.synthesize(
            _request(BackendId.SEASONAL_TTSD),
            TEXT,
            output_dir=tmp_path / "synthesis",
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert synthesis_error.value.classification == "provider_timed_out"
    assert len(synthesis_transport.requests) == 2


def test_seasonal_401_retry_keeps_original_synthesis_subdeadline(tmp_path: Path) -> None:
    credential = tmp_path / "client"
    credential.write_text(CLIENT, encoding="ascii")
    transport = DelayedSeasonal401Transport()
    adapter = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(
            base_url="https://tts.example.test",
            client_credential_file=str(credential),
            token_timeout_seconds=1,
            synthesis_timeout_seconds=0.2,
        ),
        transport=transport,
    )
    result = adapter.synthesize(
        _request(BackendId.SEASONAL_TTSD),
        TEXT,
        output_dir=tmp_path,
        deadline=time.monotonic() + 2,
        cancellation=threading.Event(),
    )
    assert result.path.exists()
    synthesis_timeouts = [
        cast(httpx.Timeout, call["timeout"]).read
        for call in transport.requests
        if str(call["url"]).endswith("/v1/syntheses")
    ]
    assert len(synthesis_timeouts) == 2
    assert synthesis_timeouts[1] < 0.2


def test_total_stream_duration_timeout_preempts_frequent_valid_progress(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="m",
            voice="v",
            synthesis_timeout_seconds=0.05,
            max_response_bytes=1024 * 1024,
        ),
        transport=FakeTransport([ProgressingResponse(delay=0.01)]),
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert error.value.classification == "provider_timed_out"


def test_successful_body_with_blocking_response_close_is_bounded(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    response = BlockingCloseResponse(body=b"audio")
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=FakeTransport([response]),
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert error.value.classification == "transport_failed"
    assert response.close_started.is_set() and response.close_cancelled.is_set()


def test_request_abort_close_is_bounded_and_global_or_provider_failure_survives(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    provider_transport = BlockingCloseTransport()
    provider_adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="m",
            voice="v",
            synthesis_timeout_seconds=0.05,
        ),
        transport=OperationTransportFactory([provider_transport]),
    )
    with pytest.raises(ProcessFailure) as provider_error:
        provider_adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path / "provider",
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert provider_error.value.classification == "provider_timed_out"
    assert provider_transport.close_started.is_set() and provider_transport.close_cancelled.is_set()

    global_transport = BlockingCloseTransport()
    global_adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=OperationTransportFactory([global_transport]),
    )
    with pytest.raises(ProcessFailure) as global_error:
        global_adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path / "global",
            deadline=time.monotonic() + 0.05,
            cancellation=threading.Event(),
        )
    assert global_error.value.classification == "timed_out"
    assert global_transport.close_cancelled.is_set()


def test_cancellation_with_blocking_abort_close_is_bounded(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    transport = BlockingCloseTransport()
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=OperationTransportFactory([transport]),
    )
    cancelled = threading.Event()
    failures: list[ProcessFailure] = []
    worker = threading.Thread(target=lambda: _capture_failure(failures, adapter, tmp_path, cancelled))
    worker.start()
    assert transport.request_started.wait(1)
    cancelled.set()
    worker.join(2)
    assert not worker.is_alive()
    assert failures[0].classification == "cancelled"
    assert transport.close_cancelled.is_set()


@pytest.mark.parametrize("status", [403, 429, 504])
def test_known_status_survives_blocking_error_body_cleanup(tmp_path: Path, status: int) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    response = BlockingCloseResponse(status_code=status, body=b"provider detail", content_type="text/plain")
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=FakeTransport([response]),
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    expected = {403: "authorization_failed", 429: "rate_limited", 504: "provider_timed_out"}
    assert error.value.classification == expected[status]
    assert response.close_cancelled.is_set()


def test_same_adapter_reuses_owned_transport_after_blocked_cleanup(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    first = BlockingCloseTransport()
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file=str(key),
            model="m",
            voice="v",
            synthesis_timeout_seconds=0.05,
        ),
        transport=OperationTransportFactory([first, FakeTransport([_audio_response()])]),
    )
    with pytest.raises(ProcessFailure):
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path / "first",
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    result = adapter.synthesize(
        _request(BackendId.OPENAI_COMPATIBLE),
        TEXT,
        output_dir=tmp_path / "second",
        deadline=time.monotonic() + 1,
        cancellation=threading.Event(),
    )
    assert result.path.exists()


def test_repeated_blocked_response_cleanup_does_not_leave_owned_work(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    for index in range(3):
        response = BlockingCloseResponse(body=b"audio")
        adapter = OpenAICompatibleAdapter(
            OpenAICompatibleConfig(
                base_url="https://api.example.test/v1",
                api_key_file=str(key),
                model="m",
                voice="v",
            ),
            transport=FakeTransport([response]),
        )
        with pytest.raises(ProcessFailure) as error:
            adapter.synthesize(
                _request(BackendId.OPENAI_COMPATIBLE),
                TEXT,
                output_dir=tmp_path / str(index),
                deadline=time.monotonic() + 1,
                cancellation=threading.Event(),
            )
        assert error.value.classification == "transport_failed"
        assert response.close_cancelled.is_set()


def test_composite_response_and_transport_cleanup_is_cancelled_together(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    response = BlockingCloseResponse(body=b"audio")
    transport = CompositeCleanupTransport(response)
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=OperationTransportFactory([transport]),
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert error.value.classification == "transport_failed"
    assert response.close_cancelled.is_set()
    assert transport.close_cancelled.is_set()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, "authorization_failed"),
        (504, "provider_timed_out"),
    ],
)
def test_composite_cleanup_does_not_replace_http_or_provider_primary(
    tmp_path: Path, status: int, expected: str
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    response = BlockingCloseResponse(status_code=status, body=b"detail", content_type="text/plain")
    transport = CompositeCleanupTransport(response)
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=OperationTransportFactory([transport]),
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=time.monotonic() + 1,
            cancellation=threading.Event(),
        )
    assert error.value.classification == expected
    assert response.close_cancelled.is_set() and transport.close_cancelled.is_set()


@pytest.mark.parametrize("fence", ["global", "cancelled"])
def test_composite_cleanup_does_not_replace_global_or_cancellation_primary(tmp_path: Path, fence: str) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    response = CompositeBlockingBodyResponse(403)
    transport = CompositeCleanupTransport(response)
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=OperationTransportFactory([transport]),
    )
    cancellation = threading.Event()
    failures: list[ProcessFailure] = []
    worker = threading.Thread(
        target=lambda: _capture_failure(
            failures,
            adapter,
            tmp_path / fence,
            cancellation,
            deadline_seconds=0.05 if fence == "global" else 2,
        )
    )
    worker.start()
    assert response.body_started.wait(1)
    if fence == "cancelled":
        cancellation.set()
    worker.join(2)
    assert not worker.is_alive()
    assert failures[0].classification == ("timed_out" if fence == "global" else "cancelled")
    assert response.close_cancelled.is_set() and transport.close_cancelled.is_set()


def test_repeated_composite_cleanup_does_not_accumulate_owned_tasks(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    for index in range(3):
        response = BlockingCloseResponse(body=b"audio")
        transport = CompositeCleanupTransport(response)
        adapter = OpenAICompatibleAdapter(
            OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
            transport=OperationTransportFactory([transport]),
        )
        with pytest.raises(ProcessFailure):
            adapter.synthesize(
                _request(BackendId.OPENAI_COMPATIBLE),
                TEXT,
                output_dir=tmp_path / str(index),
                deadline=time.monotonic() + 1,
                cancellation=threading.Event(),
            )
        assert response.close_cancelled.is_set() and transport.close_cancelled.is_set()


def test_adapter_close_uses_bounded_owned_cleanup(tmp_path: Path) -> None:
    del tmp_path
    transport = BlockingCloseTransport()
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(),
        transport=transport,
    )
    started = time.monotonic()
    with pytest.raises(ProcessFailure) as error:
        adapter.close()
    assert time.monotonic() - started < 0.5
    assert error.value.classification == "transport_failed"
    assert transport.close_started.is_set() and transport.close_cancelled.is_set()


def _capture_failure(
    failures: list[ProcessFailure],
    adapter,
    output_dir: Path,
    cancellation: threading.Event,
    deadline_seconds: float = 2,
) -> None:
    try:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=output_dir,
            deadline=time.monotonic() + deadline_seconds,
            cancellation=cancellation,
        )
    except ProcessFailure as error:
        failures.append(error)


def test_repeated_remote_timeouts_do_not_accumulate_helpers_or_poison_same_adapter(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")

    class ReusableTransport(BlockingTransport):
        def __init__(self):
            super().__init__(_audio_response())
            self.calls = 0

        async def request(self, method, url, *, headers, json, timeout):
            self.calls += 1
            if self.calls <= 3:
                self.release = asyncio.Event()
                self.release.clear()
                return await super().request(method, url, headers=headers, json=json, timeout=timeout)
            return _audio_response()

    transport = ReusableTransport()
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=transport,
    )
    for _ in range(3):
        failure: list[ProcessFailure] = []
        thread = threading.Thread(
            target=lambda failure=failure: _capture_failure(
                failure, adapter, tmp_path / "repeat", threading.Event(), deadline_seconds=0.05
            )
        )
        thread.start()
        assert transport.entered.wait(1)
        thread.join(2)
        assert not thread.is_alive()
        assert failure[0].classification == "timed_out"
        assert _tts_helper_threads() == []
        transport.entered.clear()
    # The reusable transport was aborted and joined; a later operation still runs.
    adapter.synthesize(
        _request(BackendId.OPENAI_COMPATIBLE),
        TEXT,
        output_dir=tmp_path / "success",
        deadline=time.monotonic() + 2,
        cancellation=threading.Event(),
    )
    assert _tts_helper_threads() == []


def test_cancelling_one_isolated_remote_request_does_not_break_another(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    created: list[BlockingTransport | FakeTransport] = []

    class IsolatedFactory:
        def for_operation(self):
            transport = BlockingTransport(_audio_response()) if not created else FakeTransport([_audio_response()])
            created.append(transport)
            return transport

    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=IsolatedFactory(),
    )
    cancelled = threading.Event()
    failures: list[ProcessFailure] = []
    first = threading.Thread(target=lambda: _capture_failure(failures, adapter, tmp_path / "cancelled", cancelled))
    first.start()
    for _ in range(100):
        if created:
            break
        time.sleep(0.01)
    blocking = created[0] if created else None
    assert isinstance(blocking, BlockingTransport)
    assert blocking.entered.wait(1)
    second_result: list[ProviderAudio] = []
    second = threading.Thread(
        target=lambda: second_result.append(
            adapter.synthesize(
                _request(BackendId.OPENAI_COMPATIBLE),
                TEXT,
                output_dir=tmp_path / "survivor",
                deadline=time.monotonic() + 2,
                cancellation=threading.Event(),
            )
        )
    )
    second.start()
    cancelled.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert failures[0].classification == "cancelled"
    assert len(second_result) == 1
    assert _tts_helper_threads() == []


@pytest.mark.parametrize(
    ("status", "classification"),
    [
        (400, "request_rejected"),
        (404, "request_rejected"),
        (413, "request_rejected"),
        (422, "request_rejected"),
        (502, "provider_failed"),
        (503, "provider_failed"),
        (504, "provider_timed_out"),
    ],
)
def test_remote_http_taxonomy_is_deterministic(tmp_path: Path, status: int, classification: str) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=FakeTransport([FakeResponse(status, b"provider body")]),
    )
    with pytest.raises(ProcessFailure) as error:
        adapter.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert error.value.classification == classification


def test_seasonal_raw_wav_and_openai_format_contracts_are_enforced(tmp_path: Path) -> None:
    credential = tmp_path / "client"
    credential.write_text(CLIENT, encoding="ascii")
    seasonal = SeasonalTtsdAdapter(
        SeasonalTtsdConfig(base_url="https://tts.example.test", client_credential_file=str(credential)),
        transport=FakeTransport([_token_response(), _audio_response(b"not-a-wav")]),
    )
    service = SynthesisService(provider_adapters={BackendId.SEASONAL_TTSD: seasonal})
    seasonal_result = service.synthesize(_request(BackendId.SEASONAL_TTSD), tmp_path / "seasonal.wav")
    assert seasonal_result.failure is SynthesisFailure.UNSUPPORTED_AUDIO_FORMAT

    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    raw = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v", response_format="pcm"
        ),
        transport=FakeTransport([FakeResponse(200, b"raw", "audio/pcm")]),
    )
    with pytest.raises(ProcessFailure) as raw_error:
        raw.synthesize(
            _request(BackendId.OPENAI_COMPATIBLE),
            TEXT,
            output_dir=tmp_path,
            deadline=9999999999,
            cancellation=threading.Event(),
        )
    assert raw_error.value.classification == "unsupported_audio_format"


def test_remote_profile_identity_is_public_and_part_of_common_output_profile() -> None:
    first = TTS(
        backend="seasonal_ttsd",
        voice="9",
        rate_wpm=165,
        volume=1.0,
        sample_rate=48_000,
        seasonal_ttsd_config=SeasonalTtsdConfig(
            base_url="https://private.example", client_credential_file="/private/client", profile="wav-48k-stereo"
        ),
    )
    second = TTS(
        backend="seasonal_ttsd",
        voice="9",
        rate_wpm=165,
        volume=1.0,
        sample_rate=48_000,
        seasonal_ttsd_config=SeasonalTtsdConfig(
            base_url="https://private.example", client_credential_file="/private/client", profile="other"
        ),
    )
    first_request = first.request_for(TEXT)
    second_request = second.request_for(TEXT)
    assert first_request.backend_profile_identity != second_request.backend_profile_identity
    assert "/private/client" not in first_request.backend_profile_identity
    assert "private.example" not in first_request.backend_profile_identity
    assert TEXT not in first_request.backend_profile_identity


def test_public_provider_settings_fence_lkg_but_credential_only_changes_do_not() -> None:
    from seasonalweather.tts.service import SynthesisService

    first = TTS(
        backend="openai_compatible",
        voice="alloy",
        rate_wpm=165,
        volume=1.0,
        sample_rate=48_000,
        openai_compatible_config=OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file="/private/key-a",
            model="model-a",
            voice="alloy",
            response_format="wav",
            speed=1.0,
        ),
    )
    credential_only = TTS(
        backend="openai_compatible",
        voice="alloy",
        rate_wpm=165,
        volume=1.0,
        sample_rate=48_000,
        openai_compatible_config=OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file="/private/key-b",
            model="model-a",
            voice="alloy",
            response_format="wav",
            speed=1.0,
        ),
    )
    changed_public = TTS(
        backend="openai_compatible",
        voice="alloy",
        rate_wpm=165,
        volume=1.0,
        sample_rate=48_000,
        openai_compatible_config=OpenAICompatibleConfig(
            base_url="https://api.example.test/v1",
            api_key_file="/private/key-a",
            model="model-b",
            voice="alloy",
            response_format="wav",
            speed=1.0,
        ),
    )
    first_identity = first.request_for(TEXT).backend_profile_identity
    assert first_identity == credential_only.request_for(TEXT).backend_profile_identity
    assert first_identity != changed_public.request_for(TEXT).backend_profile_identity

    # The common output-profile fence consumes this non-secret identity, so a
    # controller-owned LKG record from model-a cannot satisfy model-b.
    service = SynthesisService()
    first_request = first.request_for(TEXT)
    changed_request = changed_public.request_for(TEXT)
    assert service._output_profile(first_request) != service._output_profile(changed_request)

    accepted = AcceptedArtifactReference(
        artifact_ref="artifact:tts:remote-profile",
        path="/controller-owned/remote.wav",
        content_identity="content-identity",
        purpose=first_request.purpose,
        backend=first_request.backend,
        preprocessing_version=first_request.preprocessing_version,
        configuration_generation=first_request.configuration_generation,
        output_profile_identity=service._output_profile(
            first_request.model_copy(update={"content_identity": "content-identity"})
        ),
        artifact=ArtifactEvidence(
            sha256="sha256:" + "0" * 64,
            size_bytes=1,
            sample_rate_hz=48_000,
            channels=2,
            frame_count=1,
            duration_seconds=0.01,
        ),
        freshness_deadline_at=first_request.deadline_at + dt.timedelta(seconds=30),
    )
    service._verify_lkg_metadata(accepted, first_request.model_copy(update={"content_identity": "content-identity"}))
    with pytest.raises(ProcessFailure, match="fences"):
        service._verify_lkg_metadata(
            accepted, changed_request.model_copy(update={"content_identity": "content-identity"})
        )


def test_selected_remote_config_is_source_validated_and_local_defaults_stay_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from seasonalweather.config import load_config

    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "test-source")
    monkeypatch.setenv("NWWS_JID", "changeme@nwws-oi.weather.gov")
    monkeypatch.setenv("NWWS_PASSWORD", "CHANGEME")
    example = Path(__file__).resolve().parents[1] / "config/config.yaml"
    text = example.read_text(encoding="utf-8")
    selected = text.replace('backend: "local"', 'backend: "seasonal_ttsd"', 1)
    selected = selected.replace('base_url: ""', 'base_url: "https://tts.example.test"', 1)
    selected = selected.replace('client_credential_file: ""', 'client_credential_file: "/tmp/client"', 1)
    selected_path = tmp_path / "selected.yaml"
    selected_path.write_text(selected, encoding="utf-8")
    config = load_config(str(selected_path))
    assert config.tts.backend == "seasonal_ttsd"
    assert config.tts.seasonal_ttsd.base_url == "https://tts.example.test"

    bad_url = selected.replace("https://tts.example.test", "https://tts.example.test/?token=secret")
    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text(bad_url, encoding="utf-8")
    with pytest.raises(ValueError, match="HTTPS origin"):
        load_config(str(bad_path))


def test_remote_service_keeps_common_boundary_and_uses_local_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Provider:
        backend_id = "seasonal_ttsd"

        def synthesize(self, request, text, *, output_dir, deadline, cancellation):
            assert text == "plain normalized text"
            raise ProcessFailure("rate_limited", "provider unavailable")

        def close(self):
            pass

    service = SynthesisService(provider_adapters={BackendId.SEASONAL_TTSD: Provider()})
    monkeypatch.setattr(service, "_admit_capability", lambda *args: object())

    def local(req, output, engine, deadline, cancellation):
        assert req.backend is BackendId.LOCAL
        output.write_bytes(b"local-wav")
        return ArtifactEvidence(
            sha256="sha256:" + "0" * 64,
            size_bytes=9,
            sample_rate_hz=48_000,
            channels=2,
            frame_count=1,
            duration_seconds=0.01,
        )

    monkeypatch.setattr(service, "_run_local", local)
    request = _request(BackendId.SEASONAL_TTSD, fallback=BackendId.LOCAL)
    result = service.synthesize(request, tmp_path / "result.wav")
    assert result.fallback is not None and result.fallback.succeeded
    assert result.backend is BackendId.LOCAL


def test_service_provider_timeout_uses_local_fallback_while_global_deadline_remains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Provider:
        backend_id = "openai_compatible"

        def synthesize(self, request, text, *, output_dir, deadline, cancellation):
            del request, text, output_dir, deadline, cancellation
            raise ProcessFailure("provider_timed_out", "provider timeout")

        def close(self):
            pass

    service = SynthesisService(provider_adapters={BackendId.OPENAI_COMPATIBLE: Provider()})
    monkeypatch.setattr(service, "_admit_capability", lambda *args: object())
    monkeypatch.setattr(
        service,
        "_run_local",
        lambda *args, **kwargs: ArtifactEvidence(
            sha256="sha256:" + "0" * 64,
            size_bytes=9,
            sample_rate_hz=48_000,
            channels=2,
            frame_count=1,
            duration_seconds=0.01,
        ),
    )
    result = service.synthesize(
        _request(BackendId.OPENAI_COMPATIBLE, fallback=BackendId.LOCAL),
        tmp_path / "provider-timeout-fallback.wav",
    )
    assert result.disposition.value == "succeeded"
    assert result.backend is BackendId.LOCAL
    assert result.fallback is not None and result.fallback.succeeded


def test_actual_http_504_maps_through_service_to_provider_timeout_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-secret", encoding="ascii")
    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url="https://api.example.test/v1", api_key_file=str(key), model="m", voice="v"),
        transport=FakeTransport([FakeResponse(504, b"provider detail")]),
    )
    service = SynthesisService(provider_adapters={BackendId.OPENAI_COMPATIBLE: adapter})
    monkeypatch.setattr(service, "_admit_capability", lambda *args: object())
    monkeypatch.setattr(
        service,
        "_run_local",
        lambda *args, **kwargs: ArtifactEvidence(
            sha256="sha256:" + "0" * 64,
            size_bytes=9,
            sample_rate_hz=48_000,
            channels=2,
            frame_count=1,
            duration_seconds=0.01,
        ),
    )
    result = service.synthesize(
        _request(BackendId.OPENAI_COMPATIBLE, fallback=BackendId.LOCAL),
        tmp_path / "http-504.wav",
    )
    assert result.backend is BackendId.LOCAL
    assert result.fallback is not None and result.fallback.reason is SynthesisFailure.PROVIDER_TIMEOUT


def test_global_timeout_remains_distinct_and_disallows_fallback(tmp_path: Path) -> None:
    class Provider:
        backend_id = "openai_compatible"

        def synthesize(self, request, text, *, output_dir, deadline, cancellation):
            del request, text, output_dir, deadline, cancellation
            raise ProcessFailure("timed_out", "operation timeout")

        def close(self):
            pass

    service = SynthesisService(provider_adapters={BackendId.OPENAI_COMPATIBLE: Provider()})
    result = service.synthesize(
        _request(BackendId.OPENAI_COMPATIBLE, fallback=BackendId.LOCAL),
        tmp_path / "global-timeout.wav",
    )
    assert result.failure is SynthesisFailure.DEADLINE_EXPIRED
    assert result.fallback is None


def test_remote_success_uses_common_finalization_path_without_local_admission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Provider:
        backend_id = "openai_compatible"

        def synthesize(self, request, text, *, output_dir, deadline, cancellation):
            staged = output_dir / "provider.wav"
            output_dir.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(b"provider-audio")
            return ProviderAudio(staged, "audio/wav", "wav")

        def close(self):
            pass

    evidence = ArtifactEvidence(
        sha256="sha256:" + "1" * 64,
        size_bytes=14,
        sample_rate_hz=48_000,
        channels=2,
        frame_count=1,
        duration_seconds=0.01,
    )
    service = SynthesisService(provider_adapters={BackendId.OPENAI_COMPATIBLE: Provider()})
    calls: list[str] = []

    def normalize(path, request, raw_dir, deadline, cancellation):
        calls.append(f"normalize:{path.name}")
        return path, evidence

    def accept(request, engine, output, normalized, media, raw_dir, deadline, cancellation, reservation):
        calls.append(f"accept:{engine}:{reservation}")
        return evidence

    monkeypatch.setattr(service, "_normalize_local_audio", normalize)
    monkeypatch.setattr(service, "_accept_local_audio", accept)
    result = service.synthesize(_request(BackendId.OPENAI_COMPATIBLE), tmp_path / "result.wav")
    assert result.disposition.value == "succeeded"
    assert result.backend is BackendId.OPENAI_COMPATIBLE
    assert calls == ["normalize:provider.wav", "accept:openai_compatible:None"]


def test_token_repr_does_not_expose_access_token() -> None:
    assert ACCESS not in repr(_AccessToken(ACCESS, 1.0))
