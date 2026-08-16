#!/usr/bin/env python3
"""Bounded NWWS smoke runner using the controller-owned source boundary.

This utility intentionally knows nothing about the selected XMPP library.  It
is a diagnostic consumer of ``NwwsSource`` and therefore exercises the same
normalized contract used by the controller.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ..logging_config import setup_logging
from .source import NwwsProductEnvelope, ProductSink, build_nwws_source

log = logging.getLogger("seasonalweather.nwws_smoke")

ENV_PATH = Path("/etc/seasonalweather/seasonalweather.env")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and ((value[0] == value[-1] == "'") or (value[0] == value[-1] == '"')):
        return value[1:-1]
    return value


def load_env_file(path: Path) -> dict[str, str]:
    """Read simple, non-secret configuration values without logging them."""
    if not path.exists():
        raise FileNotFoundError(f"env file not found: {path}")
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_quotes(value)
        if key and not (key in data and data[key] and not value):
            data[key] = value
    return data


class _SmokeSink(ProductSink):
    def __init__(self) -> None:
        self.total = 0

    async def accept(self, envelope: NwwsProductEnvelope) -> None:
        self.total += 1
        log.info(
            "RX #%d source=%s identity=%s hash=%s sequence=%s wmo=%s office=%s awips=%s",
            self.total,
            envelope.source,
            envelope.identity,
            envelope.content_hash,
            envelope.sequence or "none",
            envelope.wmo_heading or "none",
            envelope.issuing_office or "none",
            envelope.awips_id or "none",
        )


async def _run(env: dict[str, str], duration_seconds: int) -> int:
    source = build_nwws_source(
        env["NWWS_JID"],
        env["NWWS_PASSWORD"],
        (env.get("NWWS_SERVER") or "nwws-oi.weather.gov").strip(),
        int((env.get("NWWS_PORT") or "5222").strip()),
        room_jid=(env.get("NWWS_ROOM") or "NWWS@conference.nwws-oi.weather.gov").strip(),
        nick=(env.get("NWWS_NICK") or "SeasonalWeatherSmoke").strip(),
        stall_seconds=60,
        muc_confirm_seconds=30,
        start_wait_seconds=25,
        join_wait_seconds=35,
        backoff_max_seconds=90,
        generation=0,
    )
    sink = _SmokeSink()
    task = asyncio.create_task(source.start(sink), name="nwws-smoke-source")
    try:
        await asyncio.sleep(duration_seconds)
    finally:
        await source.drain()
        await source.stop()
    failure = False
    try:
        await asyncio.wait_for(task, timeout=6.0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failure = True
        log.error("NWWS source ended with %s", type(exc).__name__)
    if failure:
        return 1
    log.info("NWWS smoke run stopped cleanly after %d product(s)", sink.total)
    return 0


def main() -> int:
    setup_logging()
    try:
        env = load_env_file(ENV_PATH)
        if not env.get("NWWS_JID") or not env.get("NWWS_PASSWORD"):
            log.error("Missing required NWWS credentials in %s", ENV_PATH)
            return 2
        duration = int(os.environ.get("NWWS_SMOKE_DURATION_SECONDS", "120"))
        duration = max(1, min(duration, 3_600))
        return asyncio.run(_run(env, duration))
    except KeyboardInterrupt:
        log.info("NWWS smoke run interrupted; shutdown was requested")
        return 130
    except (OSError, ValueError) as exc:
        log.error("NWWS smoke configuration failed: %s", type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
