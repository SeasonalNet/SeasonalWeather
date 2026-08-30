import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from seasonalweather.broadcast.ern_script import build_ern_relay_script


def test_ern_script_includes_test_metadata_and_area():
    ev = SimpleNamespace(
        org="EAS",
        event="RWT",
        locations=("024003", "024031"),
        tttt="0015",
        jjjhhmm="1480130",
        sender="WJON/TV",
    )

    script = build_ern_relay_script(
        ev,
        same_locations=ev.locations,
        area_text="Anne Arundel County, MD; Montgomery County, MD",
        tz=ZoneInfo("America/New_York"),
        now_utc=dt.datetime(2026, 5, 28, 2, 0, tzinfo=dt.timezone.utc),
    )

    assert (
        "The Emergency Relay Network Participant WJON/TV reports a Required Weekly Test, "
        "valid from 9:30 PM EDT on Wednesday, May 27, until 9:45 PM EDT on Wednesday, May 27, "
        "for the following areas: Anne Arundel County, MD; Montgomery County, MD."
    ) in script
    assert "This is only a test." in script
    assert "An EAS participant has issued" not in script
    assert "The message is valid from:" not in script
    assert "The message was received from:" not in script
    assert script.endswith("End of test message.")


def test_ern_script_includes_warning_metadata_without_area_lookup():
    ev = SimpleNamespace(
        org="WXR",
        event="SVR",
        locations=("024003", "024031"),
        tttt="0030",
        jjjhhmm="1480130",
        sender="WJON/TV",
    )

    script = build_ern_relay_script(
        ev,
        same_locations=ev.locations,
        tz=dt.timezone.utc,
        now_utc=dt.datetime(2026, 5, 28, 2, 0, tzinfo=dt.timezone.utc),
    )

    assert (
        "The Emergency Relay Network Participant WJON/TV reports a Severe Thunderstorm Warning, "
        "valid from 1:30 AM UTC on Thursday, May 28, until 2:00 AM UTC on Thursday, May 28, "
        "for the following areas: 024003 and 024031."
    ) in script
    assert "The National Weather Service has issued" not in script
    assert "This is only a test." not in script
    assert "authoritative CAP, NWWS, or IPAWS alert text supersedes it" not in script
