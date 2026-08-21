"""Nonblocking bounded delivery primitives for optional observability sinks."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar, cast, final

T = TypeVar("T")


@dataclass(frozen=True)
class SinkStats:
    submitted: int
    delivered: int
    dropped: int
    failed: int
    running: bool


@final
class NonBlockingSink(Generic[T]):
    """Deliver records on a bounded daemon worker without blocking the caller."""

    def __init__(
        self,
        sender: Callable[[T], None],
        *,
        max_queue: int = 256,
        name: str = "observability",
        on_failure: Callable[[str, BaseException], None] | None = None,
    ) -> None:
        if max_queue < 1 or max_queue > 10_000:
            raise ValueError("sink queue size must be between 1 and 10000")
        self._sender: Callable[[T], None] = sender
        self._on_failure = on_failure
        self._queue: queue.Queue[T | object] = queue.Queue(maxsize=max_queue)
        self._name: str = name[:64]
        self._stop: object = object()
        self._thread: threading.Thread | None = None
        self._lock: threading.Lock = threading.Lock()
        self._submitted: int = 0
        self._delivered: int = 0
        self._dropped: int = 0
        self._failed: int = 0

    @property
    def name(self) -> str:
        return self._name

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name=f"sw-{self._name}", daemon=True)
            self._thread.start()

    def submit(self, item: T) -> bool:
        with self._lock:
            self._submitted += 1
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False
        return True

    def close(self, timeout: float = 1.0) -> SinkStats:
        thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                self._queue.put_nowait(self._stop)
            except queue.Full:
                with self._lock:
                    self._dropped += 1
            thread.join(max(0.0, min(timeout, 10.0)))
        return self.stats()

    def stats(self) -> SinkStats:
        with self._lock:
            thread = self._thread
            return SinkStats(
                submitted=self._submitted,
                delivered=self._delivered,
                dropped=self._dropped,
                failed=self._failed,
                running=bool(thread is not None and thread.is_alive()),
            )

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._stop:
                    return
                self._sender(cast(T, item))
            except Exception as exc:
                with self._lock:
                    self._failed += 1
                if self._on_failure is not None:
                    self._on_failure(self._name, exc)
            else:
                with self._lock:
                    self._delivered += 1
            finally:
                self._queue.task_done()


@final
class OutputHub(Generic[T]):
    """Fan out bounded output records without coupling callers to transports."""

    def __init__(
        self,
        sinks: Mapping[str, NonBlockingSink[T]],
        *,
        on_drop: Callable[[str], None] | None = None,
    ) -> None:
        self._sinks: dict[str, NonBlockingSink[T]] = dict(sinks)
        self._on_drop: Callable[[str], None] | None = on_drop

    def start(self) -> None:
        for sink in self._sinks.values():
            sink.start()

    def submit(self, item: T) -> None:
        for name, sink in self._sinks.items():
            if not sink.submit(item) and self._on_drop is not None:
                self._on_drop(name)

    def stats(self) -> Mapping[str, SinkStats]:
        return {name: sink.stats() for name, sink in self._sinks.items()}

    def close(self, timeout: float = 1.0) -> Mapping[str, SinkStats]:
        return {name: sink.close(timeout) for name, sink in self._sinks.items()}
