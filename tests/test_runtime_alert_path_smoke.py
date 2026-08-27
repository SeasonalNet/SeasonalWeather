import asyncio
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast
from seasonalweather.alerts.cap_nws import CapAlertEvent
from seasonalweather.alerts.product import ParsedProduct
from seasonalweather.config import load_config
from seasonalweather.main import Orchestrator


def _write_test_wav(path: Path, *, duration_s: float = 0.25) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 8000
    frames = max(1, int(sample_rate * duration_s))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * frames)
    return path


class _FakeAudioOriginator:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.full_calls = []
        self.voice_calls = []

    async def render_alert_audio(self, parsed, script: str, *, same_locations=None):
        self.full_calls.append((parsed, script, list(same_locations or [])))
        path = self.base / f"full-{len(self.full_calls)}.wav"
        return _write_test_wav(path)

    async def render_voice_only_audio(self, script: str, *, prefix: str = "voice"):
        self.voice_calls.append((prefix, script))
        path = self.base / f"{prefix}-{len(self.voice_calls)}.wav"
        return _write_test_wav(path)


class _FakeTelnet:
    def __init__(self) -> None:
        self.pushed = []
        self.flushed_cycle = 0
        self.flushed_voice = 0
        self.flushed_full = 0
        self.skipped_voice = 0
        self.skipped_full = 0
        self.safe_cycle_resets = 0
        self.ops = []

    def flush_cycle(self) -> None:
        self.flushed_cycle += 1
        self.ops.append("flush_cycle")

    def reset_cycle_safely(self) -> bool:
        self.safe_cycle_resets += 1
        self.ops.append("reset_cycle_safe")
        return True

    def flush_voice_alert(self) -> None:
        self.flushed_voice += 1
        self.ops.append("flush_voice")

    def flush_full_alert(self) -> None:
        self.flushed_full += 1
        self.ops.append("flush_full")

    def skip_voice_alert(self) -> None:
        self.skipped_voice += 1
        self.ops.append("skip_voice")

    def skip_full_alert(self) -> None:
        self.skipped_full += 1
        self.ops.append("skip_full")

    def push_full_alert(self, path: str, *, meta=None) -> None:
        self.pushed.append(("full", path, meta or {}))
        self.ops.append("push_full")

    def push_voice_alert(self, path: str, *, meta=None) -> None:
        self.pushed.append(("voice", path, meta or {}))
        self.ops.append("push_voice")

    def push_alert(self, path: str, *, meta=None) -> None:
        self.push_voice_alert(path, meta=meta)


class _FakeDiscord:
    def __init__(self) -> None:
        self.calls = []

    def alert_aired(self, **kwargs) -> None:
        self.calls.append(("aired", kwargs))

    def alert_updated(self, **kwargs) -> None:
        self.calls.append(("updated", kwargs))

    def alert_expired(self, **kwargs) -> None:
        self.calls.append(("expired", kwargs))

    def alert_partial_terminal(self, **kwargs) -> None:
        self.calls.append(("partial_terminal", kwargs))


class _FakeTargetResolver:
    def __init__(self, same_codes=None) -> None:
        self.same_codes = list(same_codes or ["024031"])

    def _filter_same_locations_to_service_area(self, locations):
        allowed = set(self.same_codes)
        return [str(x) for x in (locations or []) if str(x) in allowed]

    async def _nwws_same_targets_from_texts(self, raw_text: str, official_text: str):
        return ["MDC031"], list(self.same_codes), "test", True, None

    async def _nwws_wcn_watch_same_targets_from_area_desc(self, official_text: str):
        return list(self.same_codes)

    async def _sf_area_text_from_same_codes(self, codes):
        return "; ".join(str(x) for x in (codes or []))


class _FakeRefresher:
    def __init__(self) -> None:
        self.calls = []

    def trigger_immediate(self, *_args, **_kwargs) -> None:
        self.calls.append(("trigger", _args))

    def notify_alerts_changed(self) -> None:
        self.calls.append(("alerts", ()))


class _FakeConductor:
    def __init__(self) -> None:
        self.calls = []
        self.interrupt_calls = []

    def notify_flush(self, *, reset_rotation=True, reason="") -> None:
        self.calls.append((reset_rotation, reason))

    def notify_interrupt_started(self, *, duration_s, reason="") -> None:
        self.interrupt_calls.append((duration_s, reason))


class _CapRuntime(Protocol):
    async def air_full(self, event: CapAlertEvent) -> None: ...

    async def air_voice(self, event: CapAlertEvent) -> None: ...


class _NwwsRuntime(Protocol):
    async def _handle_toneout(self, parsed: ParsedProduct) -> None: ...


class _SmokeOrchestrator(Protocol):
    audio_originator: _FakeAudioOriginator
    cap_runtime: _CapRuntime
    conductor: _FakeConductor
    discord: _FakeDiscord
    nwws_runtime: _NwwsRuntime
    refresher: _FakeRefresher
    target_resolver: _FakeTargetResolver
    targeting: _FakeTargetResolver
    telnet: _FakeTelnet

    async def _push_interrupt_audio(self, wav_path: Path, *, full: bool) -> None: ...

    def _schedule_cycle_refill(self, reason: str) -> None: ...

    def _clear_liquidsoap_queues_on_startup(self) -> None: ...


def _minimal_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "test-source")
    monkeypatch.setenv("NWWS_JID", "changeme@nwws-oi.weather.gov")
    monkeypatch.setenv("NWWS_PASSWORD", "CHANGEME")
    cfg = load_config("config/config.yaml")
    return replace(
        cfg,
        paths=replace(
            cfg.paths,
            work_dir=str(tmp_path / "work"),
            operational_state_dir=str(tmp_path / "state"),
            job_state_dir=str(tmp_path / "jobs"),
            artifact_dir=str(tmp_path / "artifacts"),
            audio_dir=str(tmp_path / "audio"),
            cache_dir=str(tmp_path / "cache"),
            config_dir=str(tmp_path / "config"),
            log_dir=str(tmp_path / "log"),
        ),
        database=replace(cfg.database, enabled=False),
        station_feed=replace(cfg.station_feed, enabled=False),
    )


def _orchestrator(tmp_path, monkeypatch) -> _SmokeOrchestrator:
    orch = Orchestrator(_minimal_config(tmp_path, monkeypatch))
    # The smoke fixture deliberately replaces production collaborators with
    # stateful fakes whose assertion fields are not part of the production
    # collaborator interfaces.
    smoke_orch = cast(_SmokeOrchestrator, cast(object, orch))
    orch.lifecycle.mark_running()
    smoke_orch.audio_originator = _FakeAudioOriginator(tmp_path / "audio")
    smoke_orch.telnet = _FakeTelnet()
    smoke_orch.discord = _FakeDiscord()
    smoke_orch.target_resolver = _FakeTargetResolver(["024031"])
    smoke_orch.targeting = smoke_orch.target_resolver
    smoke_orch.refresher = _FakeRefresher()
    smoke_orch.conductor = _FakeConductor()
    return smoke_orch


def _cap_event(*, event="Severe Thunderstorm Warning", vtec_action="NEW") -> CapAlertEvent:
    vtec = f"/O.{vtec_action}.KLWX.SV.W.0123.260614T2000Z-260614T2030Z/"
    return CapAlertEvent(
        alert_id=f"urn:oid:2.49.0.1.840.0.test-{vtec_action}",
        sent="2026-06-14T20:00:00+00:00",
        status="Actual",
        message_type="Alert",
        event=event,
        severity="Severe",
        urgency="Immediate",
        certainty="Likely",
        headline=f"{event} issued June 14 at 4:00PM EDT",
        area_desc="Montgomery County",
        description="At 4:00 PM EDT, a severe thunderstorm was located near Rockville.",
        instruction="For your protection move to an interior room on the lowest floor of a building.",
        effective="2026-06-14T20:00:00+00:00",
        onset="2026-06-14T20:00:00+00:00",
        ends="2026-06-14T20:30:00+00:00",
        expires="2026-06-14T20:30:00+00:00",
        references=[],
        same_fips=["024031"],
        parameters={"VTEC": [vtec]},
        vtec=[vtec],
    )


def _nwws_product(*, product_type: str, action: str) -> tuple[ParsedProduct, str]:
    raw = f"""WUUS51 KLWX 142000
{product_type}LWX

Severe Thunderstorm Warning
National Weather Service Baltimore MD/Washington DC
400 PM EDT Sun Jun 14 2026

MDC031-142030-
/O.{action}.KLWX.SV.W.0123.260614T2000Z-260614T2030Z/
Montgomery MD-
400 PM EDT Sun Jun 14 2026

...A SEVERE THUNDERSTORM WARNING {"REMAINS IN EFFECT" if action == "CON" else "IS IN EFFECT"} UNTIL 430 PM EDT FOR MONTGOMERY COUNTY...

At 400 PM EDT, a severe thunderstorm was located near Rockville.

PRECAUTIONARY/PREPAREDNESS ACTIONS...
Move to an interior room on the lowest floor of a building.

$$
"""
    return ParsedProduct(
        product_type=product_type, wfo="KLWX", awips_id=f"{product_type}LWX", vtec=None, raw_text=raw
    ), raw


def _mixed_wcn_product() -> ParsedProduct:
    raw = """WWUS61 KLWX 201901
WCNLWX

WATCH COUNTY NOTIFICATION FOR WATCHES 604/605
NATIONAL WEATHER SERVICE BALTIMORE MD/WASHINGTON DC
301 PM EDT THU AUG 20 2026

DCC001-MDC031-210200-
/O.CAN.KLWX.SV.A.0604.000000T0000Z-260821T0200Z/
/O.NEW.KLWX.TO.A.0605.260820T1901Z-260821T0200Z/

THE NATIONAL WEATHER SERVICE HAS ISSUED TORNADO WATCH 605 UNTIL
10 PM EDT THIS EVENING WHICH REPLACES A PORTION OF SEVERE
THUNDERSTORM WATCH 604. THE NEW WATCH IS VALID FOR THE FOLLOWING
AREAS

THE DISTRICT OF COLUMBIA

IN MARYLAND THE NEW WATCH INCLUDES 1 COUNTY

IN CENTRAL MARYLAND

MONTGOMERY

$$
"""
    return ParsedProduct(product_type="WCN", wfo="KLWX", awips_id="WCNLWX", vtec=None, raw_text=raw)


def test_cap_full_runtime_path_smoke(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)

    asyncio.run(orch.cap_runtime.air_full(_cap_event()))

    assert orch.audio_originator.full_calls
    parsed, _script, same_locations = orch.audio_originator.full_calls[0]
    assert parsed.product_type == "SVR"
    assert same_locations == ["024031"]
    assert orch.telnet.pushed
    assert orch.telnet.pushed[-1][0] == "full"
    assert orch.telnet.flushed_voice == 0
    assert orch.telnet.skipped_voice == 0
    assert orch.telnet.flushed_full == 0
    assert orch.telnet.skipped_full == 0
    assert orch.telnet.flushed_cycle == 0
    assert orch.telnet.ops == ["push_full", "reset_cycle_safe"]
    assert orch.conductor.interrupt_calls == [(0.25, "full-interrupt")]
    assert any(kind == "aired" for kind, _payload in orch.discord.calls)


def test_cap_voice_runtime_path_smoke(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)

    asyncio.run(orch.cap_runtime.air_voice(_cap_event(vtec_action="CON")))

    assert not orch.audio_originator.full_calls
    assert orch.audio_originator.voice_calls
    prefix, script = orch.audio_originator.voice_calls[0]
    assert prefix == "capvoice"
    assert "Severe Thunderstorm Warning" in script
    assert orch.telnet.pushed
    assert orch.telnet.pushed[-1][0] == "voice"
    assert orch.telnet.flushed_voice == 0
    assert orch.telnet.skipped_voice == 0
    assert orch.telnet.flushed_cycle == 0
    assert orch.telnet.ops == ["push_voice", "reset_cycle_safe"]
    assert orch.conductor.interrupt_calls == [(0.25, "voice-interrupt")]


def test_nwws_full_runtime_path_smoke(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)
    parsed, _official_text = _nwws_product(product_type="SVR", action="NEW")

    asyncio.run(orch.nwws_runtime._handle_toneout(parsed))

    assert orch.audio_originator.full_calls
    render_parsed, _script, same_locations = orch.audio_originator.full_calls[0]
    assert render_parsed.product_type == "SVR"
    assert same_locations == ["024031"]
    assert orch.telnet.pushed
    assert orch.telnet.pushed[-1][0] == "full"
    assert orch.telnet.flushed_voice == 0
    assert orch.telnet.skipped_voice == 0
    assert orch.telnet.flushed_full == 0
    assert orch.telnet.skipped_full == 0
    assert orch.telnet.flushed_cycle == 0
    assert orch.telnet.ops == ["push_full", "reset_cycle_safe"]
    assert orch.conductor.interrupt_calls == [(0.25, "full-interrupt")]


def test_nwws_voice_runtime_path_smoke(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)
    parsed, _official_text = _nwws_product(product_type="SVS", action="CON")

    asyncio.run(orch.nwws_runtime._handle_toneout(parsed))

    assert not orch.audio_originator.full_calls
    assert orch.audio_originator.voice_calls
    prefix, script = orch.audio_originator.voice_calls[0]
    assert prefix == "nwwsvoice"
    assert "severe thunderstorm warning" in script.lower()
    assert orch.telnet.pushed
    assert orch.telnet.pushed[-1][0] == "voice"
    assert orch.telnet.flushed_voice == 0
    assert orch.telnet.skipped_voice == 0
    assert orch.telnet.flushed_cycle == 0
    assert orch.telnet.ops == ["push_voice", "reset_cycle_safe"]
    assert orch.conductor.interrupt_calls == [(0.25, "voice-interrupt")]


def test_nwws_svs_discord_uses_underlying_vtec_event_code(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)
    parsed, _official_text = _nwws_product(product_type="SVS", action="CON")

    asyncio.run(orch.nwws_runtime._handle_toneout(parsed))

    assert orch.discord.calls
    kind, payload = orch.discord.calls[-1]
    assert kind == "updated"
    assert payload["code"] == "SVR"
    assert payload["event"] == "Severe Thunderstorm Warning"


def test_nwws_wcn_discord_uses_watch_vtec_event_code(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)
    raw = """WUUS61 KLWX 091830
WCNLWX

Severe Thunderstorm Watch
National Weather Service Baltimore MD/Washington DC
230 PM EDT Thu Jul 9 2026

MDC031-092000-
/O.NEW.KLWX.SV.A.0474.260709T1830Z-260710T0200Z/
Montgomery MD-
230 PM EDT Thu Jul 9 2026

...A SEVERE THUNDERSTORM WATCH IS IN EFFECT UNTIL 1000 PM EDT...

$$
"""
    parsed = ParsedProduct(product_type="WCN", wfo="KLWX", awips_id="WCNLWX", vtec=None, raw_text=raw)

    asyncio.run(orch.nwws_runtime._handle_toneout(parsed))

    assert orch.discord.calls
    kind, payload = orch.discord.calls[-1]
    assert kind == "aired"
    assert payload["code"] == "SVA"
    assert payload["event"] == "Severe Thunderstorm Watch"


def test_nwws_partial_terminal_discord_is_not_logged_as_fully_cancelled(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)
    raw = """WWUS51 KLWX 091839
SVSLWX

Severe Thunderstorm Warning
National Weather Service Baltimore MD/Washington DC
239 PM EDT Thu Jul 9 2026

MDC031-091845-
/O.CAN.KLWX.SV.W.0247.000000T0000Z-260709T1845Z/
Montgomery MD-
239 PM EDT Thu Jul 9 2026

...THE SEVERE THUNDERSTORM WARNING FOR MONTGOMERY COUNTY IS CANCELLED...

MDC013-091845-
/O.CON.KLWX.SV.W.0248.000000T0000Z-260709T1845Z/
Carroll MD-
239 PM EDT Thu Jul 9 2026

...A SEVERE THUNDERSTORM WARNING REMAINS IN EFFECT FOR CARROLL COUNTY...

$$
"""
    parsed = ParsedProduct(product_type="SVS", wfo="KLWX", awips_id="SVSLWX", vtec=None, raw_text=raw)

    asyncio.run(orch.nwws_runtime._handle_toneout(parsed))

    assert orch.discord.calls
    kind, payload = orch.discord.calls[-1]
    assert kind == "partial_terminal"
    assert payload["code"] == "SVR"
    assert payload["vtec_action"] == "CAN"
    assert payload["ended_tracks"] == ["KLWX.SV.W.0247"]
    assert payload["continuing_tracks"] == ["KLWX.SV.W.0248"]


def test_nwws_mixed_wcn_airs_full_then_terminal_voice(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)

    asyncio.run(orch.nwws_runtime._handle_toneout(_mixed_wcn_product()))

    assert len(orch.audio_originator.full_calls) == 1
    assert len(orch.audio_originator.voice_calls) == 1
    assert "Tornado Watch Number 605" in orch.audio_originator.full_calls[0][1]
    assert "Severe Thunderstorm Watch Number 604 has been cancelled" not in orch.audio_originator.full_calls[0][1]
    assert "Severe Thunderstorm Watch Number 604 has been cancelled" in orch.audio_originator.voice_calls[0][1]
    assert "Tornado Watch Number 605" not in orch.audio_originator.voice_calls[0][1]
    assert [item[0] for item in orch.telnet.pushed] == ["full", "voice"]


def test_interrupt_push_resets_only_cycle_after_full_admission(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)
    wav = _write_test_wav(tmp_path / "full.wav", duration_s=1.5)

    asyncio.run(orch._push_interrupt_audio(wav, full=True))

    assert orch.telnet.ops == ["push_full", "reset_cycle_safe"]
    assert orch.telnet.flushed_full == 0
    assert orch.telnet.flushed_voice == 0
    assert orch.telnet.skipped_full == 0
    assert orch.telnet.skipped_voice == 0
    assert orch.conductor.interrupt_calls == [(1.5, "full-interrupt")]


def test_interrupt_push_resets_only_cycle_after_voice_admission(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)
    wav = _write_test_wav(tmp_path / "voice.wav", duration_s=2.0)

    asyncio.run(orch._push_interrupt_audio(wav, full=False))

    assert orch.telnet.ops == ["push_voice", "reset_cycle_safe"]
    assert orch.telnet.flushed_full == 0
    assert orch.telnet.flushed_voice == 0
    assert orch.telnet.skipped_full == 0
    assert orch.telnet.skipped_voice == 0
    assert orch.conductor.interrupt_calls == [(2.0, "voice-interrupt")]


def test_cycle_refill_wakes_conductor(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)

    orch._schedule_cycle_refill("post-alert")

    assert orch.conductor.calls == [(True, "post-alert")]
    assert ("trigger", ("id", "status")) in orch.refresher.calls
    assert ("alerts", ()) in orch.refresher.calls


def test_interrupt_push_holds_conductor_after_guarded_cycle_reset(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)
    wav = _write_test_wav(tmp_path / "voice.wav", duration_s=3.0)

    asyncio.run(orch._push_interrupt_audio(wav, full=False))

    assert "flush_cycle" not in orch.telnet.ops
    assert orch.telnet.ops == ["push_voice", "reset_cycle_safe"]
    assert orch.conductor.interrupt_calls == [(3.0, "voice-interrupt")]


def test_startup_queue_reset_does_not_poison_liquidsoap_planes(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)

    orch._clear_liquidsoap_queues_on_startup()

    assert orch.telnet.flushed_full == 0
    assert orch.telnet.skipped_full == 0
    assert orch.telnet.flushed_voice == 0
    assert orch.telnet.skipped_voice == 0
    assert orch.telnet.flushed_cycle == 0
    assert orch.telnet.ops == []
