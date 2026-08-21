"""Static-analysis surface for the runtime-pinned httpx2 package."""

from typing import Any


class AsyncClient:
    def __init__(self, *args: object, **kwargs: object) -> None: ...

    async def __aenter__(self) -> "AsyncClient": ...

    async def __aexit__(self, *args: object) -> None: ...

    def __getattr__(self, name: str) -> Any: ...


class ASGITransport:
    def __init__(self, *args: object, **kwargs: object) -> None: ...


class Timeout:
    read: float = 0.0

    def __init__(self, *args: object, **kwargs: object) -> None: ...


class Response:
    def __getattr__(self, name: str) -> Any: ...


class TimeoutException(Exception): ...


class ConnectError(Exception):
    def __init__(self, message: str = "") -> None:
        super().__init__(message)


class ReadTimeout(TimeoutException):
    def __init__(self, message: str = "") -> None:
        super().__init__(message)


class HTTPStatusError(Exception):
    def __init__(self, message: str = "", request: object | None = None, response: object | None = None) -> None:
        super().__init__(message)
