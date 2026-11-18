from __future__ import annotations

import asyncio
import textwrap
from types import SimpleNamespace
from typing import Any, cast

import pytest

from seasonalweather.alerts.ipaws_cap import IpawsCapPoller
from seasonalweather.broadcast.ern_gwes import ErnGwesMonitor
from seasonalweather.broadcast.formatters import FormatterSubsystem
from seasonalweather.broadcast.ipaws_runtime import IpawsRuntime


@pytest.mark.parametrize("event_code", ["DMO", "RWT", "RMT"])
def test_ipaws_poller_emits_product_types_for_controller_policy(event_code: str) -> None:
    xml = textwrap.dedent(
        f"""
        <alerts xmlns="urn:oasis:names:tc:emergency:cap:1.2">
          <alert>
            <identifier>test-{event_code}</identifier>
            <sender>test@example.invalid</sender>
            <sent>2026-08-27T12:00:00-04:00</sent>
            <status>Actual</status>
            <msgType>Alert</msgType>
            <info>
              <language>en-US</language>
              <event>{event_code} test</event>
              <eventCode><valueName>SAME</valueName><value>{event_code}</value></eventCode>
              <senderName>Test Authority</senderName>
              <headline>{event_code} test alert</headline>
              <description>Controller policy decides whether this is aired.</description>
              <area>
                <areaDesc>Test area</areaDesc>
                <geocode><valueName>SAME</valueName><value>024033</value></geocode>
              </area>
            </info>
          </alert>
        </alerts>
        """
    ).strip()
    poller = IpawsCapPoller(
        out_queue=asyncio.Queue(),
        same_fips_allow=["024033"],
    )

    events = poller._parse_alerts(xml)

    assert [event.event_code for event in events] == [event_code]


def test_national_scope_bypasses_local_service_area_for_ipaws_and_ern() -> None:
    ipaws = IpawsCapPoller(
        out_queue=asyncio.Queue(),
        same_fips_allow=[],
    )
    ern = ErnGwesMonitor(
        out_queue=asyncio.Queue(),
        same_fips_allow=["024033"],
        url="https://example.invalid/stream",
    )

    assert ipaws._matches_service_area(["000000"], "NPT")
    assert ern._service_area_hit(["000000"], "NPT")
    assert not ipaws._matches_service_area(["000000"], "TOR")
    assert not ern._service_area_hit(["000000"], "TOR")


@pytest.mark.parametrize(
    ("event_code", "should_air"),
    [("EAN", True), ("EAT", True), ("NPT", True), ("TOR", False), ("RWT", False), ("RMT", False)],
)
def test_national_ipaws_event_bypasses_event_allowlist(event_code: str, should_air: bool) -> None:
    aired = []

    class _Runtime(IpawsRuntime):
        async def air_full(self, event: Any) -> None:
            aired.append(event)

    host = SimpleNamespace(
        ipaws_queue=asyncio.Queue(),
        cfg=SimpleNamespace(
            ipaws=SimpleNamespace(
                full_events=[],
                voice_events=[],
                dryrun=False,
            )
        ),
    )
    event = SimpleNamespace(
        event_code=event_code,
        event="National Periodic Test",
        message_type="Alert",
        identifier="npt-test",
        sender="IPAWS@fema.dhs.gov",
        sender_name_clean="FEMA",
        sender_name_raw="FEMA",
        same_fips=["000000"],
    )
    runtime = _Runtime(host, cast(FormatterSubsystem, object()))

    async def _run() -> None:
        task = asyncio.create_task(runtime.run())
        try:
            await host.ipaws_queue.put(event)
            await asyncio.sleep(0)
            assert aired == ([event] if should_air else [])
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(_run())
