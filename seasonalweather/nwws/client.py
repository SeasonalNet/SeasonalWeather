"""Compatibility entrypoint for the controller-owned NWWS source boundary.

New controller code must use ``seasonalweather.nwws.source``.  This small
compatibility class preserves the old disabled-before-start behavior for local
callers without retaining the former worker thread or daemon lifecycle.
"""

from __future__ import annotations

import asyncio

from .source import build_nwws_source


class _QueueSink:
    def __init__(self, queue: asyncio.Queue[str]) -> None:
        self.queue = queue

    async def accept(self, envelope) -> None:
        await self.queue.put(envelope.raw_text)


class NWWSClient:
    """Legacy queue facade; the service runtime uses ``NwwsSource`` directly."""

    def __init__(
        self,
        jid: str,
        password: str,
        server: str,
        port: int,
        out_queue: asyncio.Queue[str],
        *,
        room_jid: str = "NWWS@conference.nwws-oi.weather.gov",
        nick: str = "SeasonalWeather",
        stall_seconds: int = 60,
        muc_confirm_seconds: int = 30,
        start_wait_seconds: int = 25,
        join_wait_seconds: int = 35,
        backoff_max_seconds: int = 90,
    ) -> None:
        self._source = build_nwws_source(
            jid,
            password,
            server,
            port,
            room_jid=room_jid,
            nick=nick,
            stall_seconds=stall_seconds,
            muc_confirm_seconds=muc_confirm_seconds,
            start_wait_seconds=start_wait_seconds,
            join_wait_seconds=join_wait_seconds,
            backoff_max_seconds=backoff_max_seconds,
            generation=0,
        )
        self._queue = out_queue
        self._closing = False
        self._worker_id = 0
        self._thread = None

    def request_shutdown(self) -> None:
        self._closing = True
        request = getattr(self._source, "_stop_event", None)
        if request is not None:
            request.set()

    stop = request_shutdown

    async def run_forever(self) -> None:
        if self._closing:
            return
        self._worker_id = 1
        try:
            await self._source.start(_QueueSink(self._queue))
        finally:
            self._thread = None


__all__ = ["NWWSClient"]
