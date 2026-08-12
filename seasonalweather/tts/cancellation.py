"""Cause-preserving cancellation token for the bounded synthesis lifecycle."""

from __future__ import annotations

import threading
import time
from enum import StrEnum
from typing import Callable


class StopCause(StrEnum):
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class SynthesisStop:
    """Preserve whether the first stop request was cancellation or deadline."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._decision_lock: threading.Lock | None = None
        self._cause: StopCause | None = None
        self._requested_at: float | None = None
        self._cancelled = threading.Event()
        self._deadline = threading.Event()

    def set_decision_lock(self, lock: threading.Lock) -> None:
        """Serialize stop requests with one final publication decision."""

        self._decision_lock = lock

    def _request(self, cause: StopCause) -> None:
        decision_lock = self._decision_lock
        if decision_lock is None:
            self._set_cause(cause)
            return
        with decision_lock:
            self._set_cause(cause)

    def _set_cause(self, cause: StopCause) -> None:
        with self._lock:
            if self._cause is None:
                self._cause = cause
                self._requested_at = self._clock()
                if cause is StopCause.CANCELLED:
                    self._cancelled.set()
                else:
                    self._deadline.set()

    def cancel(self) -> None:
        self._request(StopCause.CANCELLED)

    def expire(self) -> None:
        self._request(StopCause.TIMED_OUT)

    @property
    def cause(self) -> StopCause | None:
        with self._lock:
            return self._cause

    @property
    def requested_at(self) -> float | None:
        with self._lock:
            return self._requested_at

    def is_set(self) -> bool:
        """Event-compatible stop check for legacy synchronous test doubles."""
        return self._cancelled.is_set() or self._deadline.is_set()

    def deadline_expired(self) -> bool:
        return self._deadline.is_set()


def explicit_cancellation(token: object | None) -> bool:
    cause = getattr(token, "cause", None)
    if cause is not None:
        return cause is StopCause.CANCELLED
    return bool(token is not None and getattr(token, "is_set", lambda: False)())


def deadline_expired(token: object | None) -> bool:
    cause = getattr(token, "cause", None)
    if cause is not None:
        return cause is StopCause.TIMED_OUT
    return bool(token is not None and getattr(token, "deadline_expired", lambda: False)())
