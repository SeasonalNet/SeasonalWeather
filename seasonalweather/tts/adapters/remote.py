"""Optional seasonal-ttsd and OpenAI-compatible synthesis adapters."""

from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
import stat
import time
from collections.abc import Coroutine, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, local
from typing import Any, cast

import httpx

from ...configuration.semantic_rules import remote_tts_base_url_error
from ..cancellation import explicit_cancellation
from ..failures import ProcessFailure
from ..models import SynthesisRequest
from .models import OpenAICompatibleConfig, ProviderAudio, SeasonalTtsdConfig, _AccessToken
from .transport import (
    CleanupOperation,
    HttpxTransport,
    ResponseBody,
    TransportLike,
    bounded_cleanup,
    bounded_cleanup_many,
    fenced_transport_request,
    read_bounded_response,
    remaining_timeout,
    write_bounded,
)

_CLIENT_TOKEN_RE = re.compile(r"^seasonalttsd_client_[A-Za-z0-9_-]{43,}$")
_ACCESS_TOKEN_RE = re.compile(r"^seasonalttsd_access_[A-Za-z0-9_-]{43,}$")
_MAX_CREDENTIAL_BYTES = 512
_MAX_JSON_BYTES = 64 * 1024
_TRANSPORT_CONTEXT = local()


@contextmanager
def _transport_operation():
    """Keep one provider request/response lifecycle on one private event loop."""

    if getattr(_TRANSPORT_CONTEXT, "runner", None) is not None:
        yield
        return
    runner = asyncio.Runner()
    _TRANSPORT_CONTEXT.runner = runner
    try:
        yield
    finally:
        del _TRANSPORT_CONTEXT.runner
        runner.close()


def _run_async(operation: Coroutine[Any, Any, Any]) -> Any:
    """Bridge the private async transport to the synchronous adapter boundary."""

    runner = getattr(_TRANSPORT_CONTEXT, "runner", None)
    if runner is None:
        with _transport_operation():
            return _run_async(operation)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return runner.run(operation)
    close = getattr(operation, "close", None)
    if close is not None:
        close()
    raise ProcessFailure("transport_failed", "remote transport cannot run inside an active event loop") from None


def _https_url(value: str, *, name: str) -> str:
    provider = "openai_compatible" if name.startswith("openai_compatible") else "seasonal_ttsd"
    error = remote_tts_base_url_error(provider, value)
    if error is not None:
        raise ProcessFailure("invalid_input", f"{name} {error}")
    return value.rstrip("/")


def _openai_base_url(value: str) -> str:
    base = _https_url(value, name="openai_compatible.base_url")
    return base


def _seasonal_base_url(value: str) -> str:
    base = _https_url(value, name="seasonal_ttsd.base_url")
    return base


def _bounded_file(path_value: str, *, label: str) -> str:
    if not path_value:
        raise ProcessFailure("authentication_failed", f"{label} credential file is not configured")
    path = Path(path_value)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ProcessFailure("authentication_failed", f"{label} credential file is not a regular file")
            data = os.read(descriptor, _MAX_CREDENTIAL_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError:
        raise ProcessFailure("authentication_failed", f"{label} credential file is unavailable") from None
    if len(data) > _MAX_CREDENTIAL_BYTES:
        raise ProcessFailure("authentication_failed", f"{label} credential file exceeds its size limit")
    try:
        value = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise ProcessFailure("authentication_failed", f"{label} credential file is malformed") from None
    if not value or any(character.isspace() for character in value):
        raise ProcessFailure("authentication_failed", f"{label} credential file is malformed")
    return value


def _json_body(body: ResponseBody, *, label: str) -> Mapping[str, Any]:
    if len(body.data) > _MAX_JSON_BYTES:
        raise ProcessFailure("response_too_large", f"{label} response exceeded its JSON limit")
    try:
        value = json.loads(body.data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProcessFailure("malformed_response", f"{label} response was not valid JSON") from None
    if not isinstance(value, dict):
        raise ProcessFailure("malformed_response", f"{label} response was not a JSON object")
    return value


def _status_failure(status: int, *, phase: str) -> ProcessFailure | None:
    if status in {301, 302, 303, 307, 308}:
        return ProcessFailure("redirect_rejected", f"{phase} redirect was rejected")
    if status == 401:
        return ProcessFailure("authentication_failed", f"{phase} authentication failed")
    if status == 403:
        return ProcessFailure("authorization_failed", f"{phase} authorization failed")
    if status == 429:
        return ProcessFailure("rate_limited", f"{phase} was rate limited")
    if status == 504:
        return ProcessFailure("provider_timed_out", f"{phase} timed out")
    if status in {502, 503} or status >= 500:
        return ProcessFailure("provider_failed", f"{phase} provider failure")
    if status in {400, 404, 413, 422}:
        return ProcessFailure("request_rejected", f"{phase} request was rejected")
    if status >= 400:
        return ProcessFailure("request_rejected", f"{phase} request was rejected")
    return None


def _transport_failure(exc: BaseException, *, operation_deadline: float) -> ProcessFailure:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        classification = "timed_out" if time.monotonic() >= operation_deadline else "provider_timed_out"
        return ProcessFailure(classification, "remote provider request timed out")
    if _contains_tls_error(exc):
        return ProcessFailure("tls_failed", "remote provider TLS validation failed")
    if isinstance(exc, httpx.ConnectError) and isinstance(exc.__cause__ or exc.__context__, OSError):
        return ProcessFailure("transport_failed", "remote provider connection failed")
    return ProcessFailure("transport_failed", "remote provider transport failed")


def _provider_deadline(operation_deadline: float, timeout_seconds: float) -> float:
    return min(operation_deadline, time.monotonic() + timeout_seconds)


def _phase_fence(
    provider_deadline: float,
    operation_deadline: float,
    cancellation: object,
    stage: str,
) -> None:
    if explicit_cancellation(cancellation):
        raise ProcessFailure("cancelled", f"remote synthesis was cancelled during {stage}")
    now = time.monotonic()
    if now >= operation_deadline:
        raise ProcessFailure("timed_out", f"remote synthesis deadline expired during {stage}")
    if now >= provider_deadline:
        raise ProcessFailure("provider_timed_out", f"remote provider deadline expired during {stage}")


def _contains_tls_error(error: BaseException, *, maximum_depth: int = 8) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(maximum_depth):
        if current is None or id(current) in seen:
            return False
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _seasonal_synthesis(
    adapter: SeasonalTtsdAdapter,
    base_url: str,
    token: str,
    request: SynthesisRequest,
    text: str,
    output_dir: Path,
    operation_deadline: float,
    synthesis_deadline: float,
    cancellation: object,
) -> ProviderAudio:
    for auth_attempt in range(2):
        response = adapter._request(
            "POST",
            f"{base_url}/v1/syntheses",
            headers={"Authorization": f"Bearer {token}", "Accept": "audio/wav"},
            payload={
                "input": {"type": "text", "content": text},
                "voice": adapter.config.voice,
                "profile": adapter.config.profile,
            },
            deadline=synthesis_deadline,
            operation_deadline=operation_deadline,
            connect_timeout=adapter.config.connect_timeout_seconds,
            total_timeout=adapter.config.synthesis_timeout_seconds,
            cancellation=cancellation,
        )
        failure = _status_failure(response.status_code, phase="seasonal_ttsd synthesis")
        if failure is not None:
            _run_async(
                read_bounded_response(
                    response,
                    maximum_bytes=adapter.config.max_error_bytes,
                    deadline=synthesis_deadline,
                    operation_deadline=operation_deadline,
                    cancellation=cancellation,
                    error_classification="transport_failed",
                    tolerate_body_errors=True,
                )
            )
            if failure.classification == "authentication_failed" and auth_attempt == 0:
                refresh_deadline = min(
                    synthesis_deadline,
                    _provider_deadline(operation_deadline, adapter.config.token_timeout_seconds),
                )
                token = adapter._access_token(
                    operation_deadline,
                    refresh_deadline,
                    cancellation,
                    force_refresh=True,
                    stale_token=token,
                )
                continue
            raise failure
        body = _run_async(
            read_bounded_response(
                response,
                maximum_bytes=adapter.config.max_response_bytes,
                deadline=synthesis_deadline,
                operation_deadline=operation_deadline,
                cancellation=cancellation,
                error_classification="transport_failed",
            )
        )
        if not body.data or body.content_type.lower().split(";", 1)[0].strip() != "audio/wav":
            raise ProcessFailure("unsupported_audio_format", "seasonal_ttsd returned unsupported audio")
        output = output_dir / "remote.wav"
        output_dir.mkdir(parents=True, exist_ok=True)
        write_bounded(
            output,
            body.data,
            maximum_bytes=adapter.config.max_response_bytes,
            deadline=synthesis_deadline,
            operation_deadline=operation_deadline,
            cancellation=cancellation,
        )
        return ProviderAudio(output, "audio/wav", "wav")
    raise ProcessFailure("authentication_failed", "seasonal_ttsd authentication failed")


def _exchange_seasonal_token(
    adapter: SeasonalTtsdAdapter,
    operation_deadline: float,
    token_deadline: float,
    cancellation: object,
    now: float,
) -> _AccessToken:
    credential = _bounded_file(adapter.config.client_credential_file, label="seasonal_ttsd")
    if not _CLIENT_TOKEN_RE.fullmatch(credential):
        raise ProcessFailure("authentication_failed", "seasonal_ttsd credential file is malformed")
    response = adapter._request(
        "POST",
        f"{_seasonal_base_url(adapter.config.base_url)}/v1/auth/token",
        headers={"Authorization": f"Bearer {credential}", "Accept": "application/json"},
        payload={
            "requested_scopes": ["tts:synthesize"],
            "requested_prefixes": ["/v1/syntheses"],
            "ttl_seconds": adapter.config.token_ttl_seconds,
        },
        deadline=token_deadline,
        operation_deadline=operation_deadline,
        connect_timeout=adapter.config.connect_timeout_seconds,
        total_timeout=adapter.config.token_timeout_seconds,
        cancellation=cancellation,
    )
    failure = _status_failure(response.status_code, phase="seasonal_ttsd token exchange")
    body = _run_async(
        read_bounded_response(
            response,
            maximum_bytes=adapter.config.max_error_bytes if failure is not None else _MAX_JSON_BYTES,
            deadline=token_deadline,
            operation_deadline=operation_deadline,
            cancellation=cancellation,
            error_classification="transport_failed",
            tolerate_body_errors=failure is not None,
        )
    )
    if failure is not None:
        raise failure
    return _parse_seasonal_token(_json_body(body, label="seasonal_ttsd token"), now)


def _parse_seasonal_token(payload: Mapping[str, Any], now: float) -> _AccessToken:
    token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not isinstance(token, str) or not _ACCESS_TOKEN_RE.fullmatch(token):
        raise ProcessFailure("malformed_response", "seasonal_ttsd token response was malformed")
    if not isinstance(expires_in, int) or not 1 <= expires_in <= 86_400:
        raise ProcessFailure("malformed_response", "seasonal_ttsd token lifetime was malformed")
    _validate_seasonal_token_claims(payload)
    return _AccessToken(token, now + expires_in)


def _validate_seasonal_token_claims(payload: Mapping[str, Any]) -> None:
    if payload.get("token_type") != "Bearer" or not isinstance(payload.get("expires_at"), str):
        raise ProcessFailure("malformed_response", "seasonal_ttsd token type was malformed")
    scopes = payload.get("scopes")
    prefixes = payload.get("allowed_prefixes")
    if not isinstance(scopes, list) or "tts:synthesize" not in scopes:
        raise ProcessFailure("authorization_failed", "seasonal_ttsd token scope was insufficient")
    if not isinstance(prefixes, list) or "/v1/syntheses" not in prefixes:
        raise ProcessFailure("authorization_failed", "seasonal_ttsd token route was insufficient")


def _usable_access_token(
    token: _AccessToken | None,
    now: float,
    refresh_margin: int,
    force_refresh: bool,
    stale_token: str | None,
) -> str | None:
    if token is None or now + refresh_margin >= token.expires_at:
        return None
    if not force_refresh or stale_token is None or token.value != stale_token:
        return token.value
    return None


class _OwnedResponse:
    """Response whose per-operation transport is reclaimed with the body."""

    def __init__(self, response: Any, cleanup: Any) -> None:
        self._response = response
        self._cleanup = cleanup
        self.status_code = response.status_code
        self.headers = response.headers
        self._closed = False

    def aiter_bytes(self, chunk_size: int = 65_536):
        return self._response.aiter_bytes(chunk_size)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        operations: tuple[CleanupOperation, ...] = (self._response.aclose,)
        if self._cleanup is not None:
            operations = (self._response.aclose, self._cleanup)
        if not await bounded_cleanup_many(operations):
            raise ProcessFailure("transport_failed", "remote provider response cleanup failed") from None


class _RemoteAdapter:
    backend_id = "remote"

    def __init__(self, *, transport: TransportLike | None = None, verify_tls: bool = True) -> None:
        self._transport = transport
        self._verify_tls = verify_tls
        self._transport_lock = Lock()

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: object,
        deadline: float,
        operation_deadline: float | None = None,
        connect_timeout: float,
        total_timeout: float,
        cancellation: object,
    ):
        if explicit_cancellation(cancellation):
            raise ProcessFailure("cancelled", "remote synthesis was cancelled")
        return _run_async(
            self._request_async(
                method,
                url,
                headers=headers,
                payload=payload,
                deadline=deadline,
                operation_deadline=deadline if operation_deadline is None else operation_deadline,
                connect_timeout=connect_timeout,
                total_timeout=total_timeout,
                cancellation=cancellation,
            )
        )

    async def _request_async(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: object,
        deadline: float,
        operation_deadline: float,
        connect_timeout: float,
        total_timeout: float,
        cancellation: object,
    ) -> _OwnedResponse:
        transport, owned = self._new_transport()
        response = None
        try:
            response = await fenced_transport_request(
                transport,
                method,
                url,
                headers=headers,
                payload=payload,
                timeout=remaining_timeout(
                    deadline,
                    connect=connect_timeout,
                    total=total_timeout,
                    operation_deadline=operation_deadline,
                ),
                deadline=deadline,
                operation_deadline=operation_deadline,
                cancellation=cancellation,
                abort=transport.close if owned else None,
            )
            return _OwnedResponse(response, transport.close if owned else None)
        except ProcessFailure:
            raise
        except BaseException as exc:
            raise _transport_failure(exc, operation_deadline=operation_deadline) from None
        finally:
            if response is None and owned:
                await bounded_cleanup(transport.close)

    def _new_transport(self) -> tuple[TransportLike, bool]:
        with self._transport_lock:
            if self._transport is None:
                return HttpxTransport(verify_tls=self._verify_tls), True
            factory = getattr(self._transport, "for_operation", None)
            if factory is not None:
                return cast(TransportLike, factory()), True
            return self._transport, False

    def close(self) -> None:
        transport = self._transport
        if transport is not None and not _run_async(bounded_cleanup(transport.close)):
            raise ProcessFailure("transport_failed", "remote provider transport cleanup failed") from None


class SeasonalTtsdAdapter(_RemoteAdapter):
    backend_id = "seasonal_ttsd"

    def __init__(self, config: SeasonalTtsdConfig, *, transport: TransportLike | None = None) -> None:
        super().__init__(transport=transport, verify_tls=config.verify_tls)
        self.config = config
        self._token: _AccessToken | None = None
        self._token_lock = Lock()

    def synthesize(
        self,
        request: SynthesisRequest,
        text: str,
        *,
        output_dir: Path,
        deadline: float,
        cancellation: object,
    ) -> ProviderAudio:
        with _transport_operation():
            return self._synthesize(request, text, output_dir=output_dir, deadline=deadline, cancellation=cancellation)

    def _synthesize(
        self,
        request: SynthesisRequest,
        text: str,
        *,
        output_dir: Path,
        deadline: float,
        cancellation: object,
    ) -> ProviderAudio:
        base_url = _seasonal_base_url(self.config.base_url)
        if len(text.encode("utf-8")) > self.config.max_input_bytes:
            raise ProcessFailure("input_limit", "remote synthesis input exceeded its size limit")
        token_deadline = _provider_deadline(deadline, self.config.token_timeout_seconds)
        token = self._access_token(deadline, token_deadline, cancellation, force_refresh=False)
        synthesis_deadline = _provider_deadline(deadline, self.config.synthesis_timeout_seconds)
        return _seasonal_synthesis(
            self, base_url, token, request, text, output_dir, deadline, synthesis_deadline, cancellation
        )

    def _access_token(
        self,
        operation_deadline: float,
        provider_deadline: float,
        cancellation: object,
        *,
        force_refresh: bool,
        stale_token: str | None = None,
    ) -> str:
        while True:
            _phase_fence(provider_deadline, operation_deadline, cancellation, "remote authentication")
            acquired = self._token_lock.acquire(timeout=min(0.05, max(0.001, provider_deadline - time.monotonic())))
            if acquired:
                break
            _phase_fence(provider_deadline, operation_deadline, cancellation, "remote authentication")
        try:
            now = time.monotonic()
            cached = _usable_access_token(
                self._token, now, self.config.refresh_margin_seconds, force_refresh, stale_token
            )
            if cached is not None:
                return cached
            self._token = _exchange_seasonal_token(self, operation_deadline, provider_deadline, cancellation, now)
            return self._token.value
        finally:
            self._token_lock.release()


class OpenAICompatibleAdapter(_RemoteAdapter):
    backend_id = "openai_compatible"

    def __init__(self, config: OpenAICompatibleConfig, *, transport: TransportLike | None = None) -> None:
        super().__init__(transport=transport, verify_tls=config.verify_tls)
        self.config = config

    def synthesize(
        self,
        request: SynthesisRequest,
        text: str,
        *,
        output_dir: Path,
        deadline: float,
        cancellation: object,
    ) -> ProviderAudio:
        with _transport_operation():
            return self._synthesize(request, text, output_dir=output_dir, deadline=deadline, cancellation=cancellation)

    def _synthesize(
        self,
        request: SynthesisRequest,
        text: str,
        *,
        output_dir: Path,
        deadline: float,
        cancellation: object,
    ) -> ProviderAudio:
        del request
        try:
            if len(text.encode("utf-8")) > self.config.max_input_bytes:
                raise ProcessFailure("input_limit", "remote synthesis input exceeded its size limit")
            synthesis_deadline = _provider_deadline(deadline, self.config.synthesis_timeout_seconds)
            api_key = _bounded_file(self.config.api_key_file, label="openai_compatible")
            base_url = _openai_base_url(self.config.base_url)
            response = self._request(
                "POST",
                f"{base_url}/audio/speech",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "audio/*"},
                payload={
                    "model": self.config.model,
                    "voice": self.config.voice,
                    "input": text,
                    "response_format": self.config.response_format,
                    "speed": self.config.speed,
                },
                deadline=synthesis_deadline,
                operation_deadline=deadline,
                connect_timeout=self.config.connect_timeout_seconds,
                total_timeout=self.config.synthesis_timeout_seconds,
                cancellation=cancellation,
            )
            failure = _status_failure(response.status_code, phase="OpenAI-compatible synthesis")
            if failure is not None:
                _run_async(
                    read_bounded_response(
                        response,
                        maximum_bytes=self.config.max_error_bytes,
                        deadline=synthesis_deadline,
                        operation_deadline=deadline,
                        cancellation=cancellation,
                        error_classification="transport_failed",
                        tolerate_body_errors=True,
                    )
                )
                raise failure
            body = _run_async(
                read_bounded_response(
                    response,
                    maximum_bytes=self.config.max_response_bytes,
                    deadline=synthesis_deadline,
                    operation_deadline=deadline,
                    cancellation=cancellation,
                    error_classification="transport_failed",
                )
            )
            media_type = body.content_type.lower().split(";", 1)[0].strip()
            formats = {
                "wav": "audio/wav",
                "mp3": "audio/mpeg",
                "flac": "audio/flac",
                "opus": "audio/opus",
                "aac": "audio/aac",
            }
            expected_media = formats.get(self.config.response_format)
            if not body.data or expected_media is None or media_type != expected_media:
                raise ProcessFailure(
                    "unsupported_audio_format", "OpenAI-compatible provider returned unsupported audio"
                )
            output_dir.mkdir(parents=True, exist_ok=True)
            output = output_dir / f"remote.{self.config.response_format}"
            write_bounded(
                output,
                body.data,
                maximum_bytes=self.config.max_response_bytes,
                deadline=synthesis_deadline,
                operation_deadline=deadline,
                cancellation=cancellation,
            )
            return ProviderAudio(output, media_type, self.config.response_format)
        finally:
            pass
