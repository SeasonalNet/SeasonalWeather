"""Outbound SWWP transport boundary used by the worker process."""

from __future__ import annotations

from collections.abc import Awaitable
from importlib import import_module
from typing import Protocol, cast

from ..swwp.constants import DEFAULT_LIMITS, SUBPROTOCOL, ProtocolLimits


class WorkerConnection(Protocol):
    async def send(self, data: bytes) -> None: ...

    async def recv(self) -> bytes | str | None: ...

    async def close(self) -> None: ...


class WorkerTransport(Protocol):
    async def connect(self) -> WorkerConnection: ...


class _WebSocketLike(Protocol):
    async def send(self, data: bytes) -> None: ...

    async def recv(self) -> bytes | str: ...

    async def close(self) -> None: ...


class _WebSocketsModule(Protocol):
    def connect(
        self,
        url: str,
        **kwargs: object,
    ) -> Awaitable[_WebSocketLike]: ...


class WebSocketWorkerTransport:
    """Connect to the controller using the exact SWWP subprotocol."""

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        limits: ProtocolLimits = DEFAULT_LIMITS,
    ) -> None:
        if not url.startswith(("ws://", "wss://")):
            raise ValueError("worker controller URL must use ws:// or wss://")
        self.url = url
        self.token = token.strip()
        self.limits = limits

    async def connect(self) -> WorkerConnection:
        try:
            websockets = cast(_WebSocketsModule, cast(object, import_module("websockets")))
        except ImportError as exc:
            raise RuntimeError("websockets is required by worker images") from exc
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        connection = await websockets.connect(
            self.url,
            subprotocols=[SUBPROTOCOL],
            max_size=self.limits.max_message_bytes,
            additional_headers=headers,
        )
        return _WebSocketConnection(connection)


class _WebSocketConnection:
    def __init__(self, connection: _WebSocketLike) -> None:
        self._connection = connection

    async def send(self, data: bytes) -> None:
        await self._connection.send(data)

    async def recv(self) -> bytes | str | None:
        return await self._connection.recv()

    async def close(self) -> None:
        await self._connection.close()
