"""Strict, bounded async HTTP transport used only by remote TTS adapters."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable, Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import httpx2

from ..cancellation import explicit_cancellation
from ..failures import ProcessFailure

MAX_CHUNK_BYTES = 65_536
TRANSPORT_CLEANUP_GRACE_SECONDS = 0.05
CleanupOperation = Callable[[], Coroutine[Any, Any, object]]


class ResponseLike(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def aiter_bytes(self, chunk_size: int = MAX_CHUNK_BYTES) -> AsyncIterable[bytes]: ...

    async def aclose(self) -> None: ...


class TransportLike(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: object,
        timeout: httpx2.Timeout,
    ) -> ResponseLike: ...

    async def close(self) -> None: ...


class HttpxTransport:
    """One per-operation client with TLS verification and redirects disabled."""

    def __init__(self, *, verify_tls: bool) -> None:
        if not verify_tls:
            raise ValueError("remote TTS requires strict TLS verification")
        self._client = httpx2.AsyncClient(verify=True, follow_redirects=False)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: object,
        timeout: httpx2.Timeout,
    ) -> ResponseLike:
        request = self._client.build_request(method, url, headers=headers, json=json, timeout=timeout)
        return cast(ResponseLike, await self._client.send(request, stream=True))

    async def close(self) -> None:
        await self._client.aclose()

    def for_operation(self) -> HttpxTransport:
        """Create an isolated client so one cancellation cannot affect peers."""
        return HttpxTransport(verify_tls=True)


@dataclass(frozen=True, slots=True)
class ResponseBody:
    data: bytes
    content_type: str


def remaining_timeout(
    deadline: float,
    *,
    connect: float,
    total: float,
    operation_deadline: float | None = None,
) -> httpx2.Timeout:
    _transport_fence(deadline, None, "transport", operation_deadline=operation_deadline)
    effective_operation_deadline = deadline if operation_deadline is None else operation_deadline
    remaining = min(deadline, effective_operation_deadline) - time.monotonic()
    return httpx2.Timeout(
        timeout=min(remaining, max(0.001, total)),
        connect=min(remaining, max(0.001, connect)),
        read=remaining,
        write=remaining,
        pool=remaining,
    )


async def fenced_transport_request(
    transport: TransportLike,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: object,
    timeout: httpx2.Timeout,
    deadline: float,
    operation_deadline: float | None = None,
    cancellation: object,
    abort: CleanupOperation | None = None,
) -> ResponseLike:
    """Run one owned async request and reap its task on every exit."""

    _transport_fence(deadline, cancellation, "request", operation_deadline=operation_deadline)
    abort_operation = abort or _noop_close
    task = asyncio.create_task(transport.request(method, url, headers=headers, json=payload, timeout=timeout))
    try:
        return cast(
            ResponseLike,
            await _await_fenced(
                task,
                deadline,
                cancellation,
                "response headers",
                abort_operation,
                operation_deadline=operation_deadline,
            ),
        )
    except BaseException:
        await _cancel_and_reap(task, abort_operation)
        raise


async def _noop_close() -> None:
    return None


async def _await_fenced(
    task: asyncio.Task[object],
    deadline: float,
    cancellation: object,
    stage: str,
    abort: CleanupOperation,
    *,
    operation_deadline: float | None = None,
) -> object:
    while True:
        _transport_fence(deadline, cancellation, stage, operation_deadline=operation_deadline)
        remaining = min(
            0.05,
            max(
                0.001,
                min(deadline, deadline if operation_deadline is None else operation_deadline) - time.monotonic(),
            ),
        )
        done, _ = await asyncio.wait({task}, timeout=remaining)
        if done:
            return task.result()
        failure = _deadline_failure(deadline, cancellation, stage, operation_deadline=operation_deadline)
        if failure is not None:
            await _cancel_and_reap(task, abort)
            raise failure


async def _cancel_and_reap(task: asyncio.Task[object], abort: CleanupOperation) -> None:
    """Cancel owned work promptly, then boundedly close and reap its I/O."""

    abort_task = _start_task(abort)
    if not task.done():
        task.cancel()
    await _reap_tasks((task, abort_task) if abort_task is not None else (task,))


async def _safe_await(operation: CleanupOperation) -> bool:
    task = asyncio.create_task(operation())
    return await _reap_tasks((task,))


async def bounded_cleanup(operation: CleanupOperation) -> bool:
    """Run one owned close operation under the private cleanup envelope."""

    return await _safe_await(operation)


async def bounded_cleanup_many(operations: tuple[CleanupOperation, ...]) -> bool:
    """Run owned cleanup stages together so cancellation reaches every stage."""

    tasks = tuple(asyncio.create_task(operation()) for operation in operations)
    return await _reap_tasks(tasks)


def _start_task(operation: CleanupOperation) -> asyncio.Task[object] | None:
    try:
        return asyncio.create_task(operation())
    except BaseException:
        return None


async def _cancel_and_join(tasks: set[asyncio.Task[object]]) -> None:
    for task in tasks:
        task.cancel()
    with suppress(BaseException):
        await asyncio.gather(*tasks, return_exceptions=True)


async def _reap_tasks(tasks: tuple[asyncio.Task[object], ...]) -> bool:
    """Reap owned tasks under one private cleanup grace envelope."""

    active = {task for task in tasks if not task.done()}
    if not active:
        return all(not task.cancelled() and task.exception() is None for task in tasks)
    try:
        done, pending = await asyncio.wait(active, timeout=TRANSPORT_CLEANUP_GRACE_SECONDS)
    except asyncio.CancelledError:
        await _cancel_and_join(active)
        raise
    await _cancel_and_join(pending)
    return all(not task.cancelled() and task.exception() is None for task in tasks)


def _deadline_failure(
    deadline: float,
    cancellation: object,
    stage: str,
    *,
    operation_deadline: float | None = None,
) -> ProcessFailure | None:
    if explicit_cancellation(cancellation):
        return ProcessFailure("cancelled", f"remote synthesis was cancelled during {stage}")
    now = time.monotonic()
    if operation_deadline is not None and now >= operation_deadline:
        return ProcessFailure("timed_out", f"remote synthesis deadline expired during {stage}")
    if now >= deadline:
        return ProcessFailure("provider_timed_out", f"remote provider deadline expired during {stage}")
    return None


def _transport_fence(
    deadline: float,
    cancellation: object,
    stage: str,
    *,
    operation_deadline: float | None = None,
) -> None:
    failure = _deadline_failure(deadline, cancellation, stage, operation_deadline=operation_deadline)
    if failure is not None:
        raise failure


async def read_bounded_response(
    response: ResponseLike,
    *,
    maximum_bytes: int,
    deadline: float,
    operation_deadline: float | None = None,
    cancellation: object,
    error_classification: str,
    tolerate_body_errors: bool = False,
) -> ResponseBody:
    """Consume a provider body in bounded chunks with operation fences."""

    primary: BaseException | None = None
    try:
        _validate_declared_size(response, maximum_bytes, tolerate_body_errors=tolerate_body_errors)
        data = await _read_response_chunks(
            response,
            maximum_bytes,
            deadline,
            operation_deadline=operation_deadline,
            cancellation=cancellation,
            tolerate_body_errors=tolerate_body_errors,
        )
        return ResponseBody(data, response.headers.get("content-type", ""))
    except ProcessFailure as error:
        primary = error
        if tolerate_body_errors and error.classification not in {"cancelled", "timed_out", "provider_timed_out"}:
            return ResponseBody(b"", response.headers.get("content-type", ""))
        raise
    except Exception as error:
        primary = error
        if tolerate_body_errors:
            return ResponseBody(b"", response.headers.get("content-type", ""))
        raise ProcessFailure(error_classification, "provider response could not be read") from None
    finally:
        closed = await _safe_await(response.aclose)
        if not closed and primary is None and not tolerate_body_errors:
            raise ProcessFailure("transport_failed", "remote provider response cleanup failed") from None


def _validate_declared_size(response: ResponseLike, maximum_bytes: int, *, tolerate_body_errors: bool) -> None:
    declared = response.headers.get("content-length")
    if declared is None:
        return
    try:
        oversized = int(declared) > maximum_bytes
    except ValueError:
        if tolerate_body_errors:
            return
        raise ProcessFailure("malformed_response", "provider response length was malformed") from None
    if oversized and not tolerate_body_errors:
        raise ProcessFailure("response_too_large", "provider response exceeded its size limit")


async def _read_response_chunks(
    response: ResponseLike,
    maximum_bytes: int,
    deadline: float,
    operation_deadline: float | None,
    cancellation: object,
    *,
    tolerate_body_errors: bool,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    iterator = response.aiter_bytes(MAX_CHUNK_BYTES).__aiter__()
    while True:
        _transport_fence(deadline, cancellation, "response body", operation_deadline=operation_deadline)

        async def next_chunk() -> bytes:
            return await iterator.__anext__()

        next_task = asyncio.create_task(next_chunk())
        try:
            chunk = await _await_fenced(
                next_task,
                deadline,
                cancellation,
                "response body",
                response.aclose,
                operation_deadline=operation_deadline,
            )
        except StopAsyncIteration:
            return b"".join(chunks)
        except ProcessFailure:
            await _cancel_and_reap(next_task, response.aclose)
            raise
        except BaseException:
            await _cancel_and_reap(next_task, response.aclose)
            if tolerate_body_errors:
                return b""
            raise
        if not isinstance(chunk, bytes):
            if tolerate_body_errors:
                return b""
            raise ProcessFailure("malformed_response", "provider response contained an invalid body chunk")
        size += len(chunk)
        if size > maximum_bytes:
            if tolerate_body_errors:
                await _safe_await(response.aclose)
                return b""
            raise ProcessFailure("response_too_large", "provider response exceeded its size limit")
        chunks.append(chunk)


def write_bounded(
    path: Path,
    data: bytes,
    *,
    maximum_bytes: int,
    deadline: float,
    operation_deadline: float | None = None,
    cancellation: object,
) -> None:
    if len(data) > maximum_bytes:
        raise ProcessFailure("response_too_large", "provider response exceeded its size limit")
    _transport_fence(deadline, cancellation, "staged write", operation_deadline=operation_deadline)
    with path.open("wb") as handle:
        for offset in range(0, len(data), MAX_CHUNK_BYTES):
            _transport_fence(deadline, cancellation, "staged write", operation_deadline=operation_deadline)
            handle.write(data[offset : offset + MAX_CHUNK_BYTES])
    _transport_fence(deadline, cancellation, "staged write", operation_deadline=operation_deadline)
