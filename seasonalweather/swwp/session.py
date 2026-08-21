"""Shared deterministic session mechanics without transport or queue authority."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable

from .constants import DEFAULT_LIMITS, ProtocolErrorCategory, ProtocolLimits
from .messages import Envelope, Payload, ProtocolErrorPayload


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def message_fingerprint(envelope: Envelope) -> str:
    raw = envelope.model_dump(mode="json")
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class SessionMachine:
    """Bounded duplicate tracking and envelope construction for one live session."""

    def __init__(
        self,
        *,
        clock: Callable[[], dt.datetime] = _utc_now,
        id_factory: Callable[[str], str],
        limits: ProtocolLimits = DEFAULT_LIMITS,
        traceparent: str | None = None,
    ) -> None:
        self.clock = clock
        self.id_factory = id_factory
        self.limits = limits
        self.traceparent = traceparent
        self._seen: OrderedDict[str, tuple[str, tuple[Envelope, ...]]] = OrderedDict()

    def envelope(
        self,
        payload: Payload,
        *,
        session_id: str | None = None,
        worker_id: str | None = None,
        worker_instance_id: str | None = None,
        controller_epoch: int | None = None,
        worker_epoch: int | None = None,
    ) -> Envelope:
        return Envelope(
            message_type=payload.message_type,
            message_id=self.id_factory("message"),
            sent_at=self.clock(),
            session_id=session_id,
            worker_id=worker_id,
            worker_instance_id=worker_instance_id,
            controller_epoch=controller_epoch,
            worker_epoch=worker_epoch,
            traceparent=self.traceparent,
            payload=payload,
        )

    def replay(self, incoming: Envelope) -> tuple[Envelope, ...] | None:
        prior = self._seen.get(incoming.message_id)
        if prior is None:
            return None
        fingerprint, responses = prior
        if fingerprint != message_fingerprint(incoming):
            raise ValueError("conflicting duplicate message identity")
        return responses

    def remember(self, incoming: Envelope, responses: tuple[Envelope, ...]) -> None:
        self._seen[incoming.message_id] = (message_fingerprint(incoming), responses)
        self._seen.move_to_end(incoming.message_id)
        while len(self._seen) > self.limits.max_retained_errors * 8:
            self._seen.popitem(last=False)

    def reset_message_sequence(self) -> None:
        self._seen.clear()

    def error(
        self,
        category: ProtocolErrorCategory,
        summary: str,
        *,
        correlated: str | None,
        fatal: bool,
        session_id: str | None,
        worker_id: str | None,
        worker_instance_id: str | None,
        controller_epoch: int | None,
        worker_epoch: int | None,
    ) -> Envelope:
        return self.envelope(
            ProtocolErrorPayload(
                category=category,
                summary=summary[:256],
                correlated_message_id=correlated,
                fatal=fatal,
            ),
            session_id=session_id,
            worker_id=worker_id,
            worker_instance_id=worker_instance_id,
            controller_epoch=controller_epoch,
            worker_epoch=worker_epoch,
        )
