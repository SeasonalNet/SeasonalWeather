from __future__ import annotations

import asyncio
import textwrap

import pytest

from seasonalweather.alerts.ipaws_cap import IpawsCapPoller


@pytest.mark.parametrize("event_code", ["DMO", "RWT", "RMT"])
def test_ipaws_poller_emits_product_types_for_controller_policy(event_code: str, tmp_path) -> None:
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
        ledger_path=str(tmp_path / "ipaws-ledger.json"),
    )

    events = poller._parse_alerts(xml)

    assert [event.event_code for event in events] == [event_code]
