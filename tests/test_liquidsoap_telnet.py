import base64
from pathlib import Path

from seasonalweather.liquidsoap_telnet import LiquidsoapTelnet


def _wire_fake_send(tn: LiquidsoapTelnet, help_text: str):
    commands: list[str] = []

    def fake_send(command: str, *, read_deadline=None):
        commands.append(command)
        if command == "help":
            return help_text
        return "OK\nEND\n"

    object.__setattr__(tn, "_send", fake_send)
    return commands


def test_liquidsoap_telnet_prefers_seasonalweather_aliases(tmp_path: Path) -> None:
    wav = tmp_path / "alert.wav"
    wav.write_bytes(b"RIFFfakeWAVE")
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands = _wire_fake_send(
        tn,
        """
| sw.cycle.push <uri>
| sw.cycle.push_b64 <base64-uri>
| sw.cycle.skip
| sw.cycle.reset
| sw.voice_alert.push <uri>
| sw.voice_alert.push_b64 <base64-uri>
| sw.voice_alert.skip
| sw.full_alert.push <uri>
| sw.full_alert.push_b64 <base64-uri>
| sw.full_alert.skip
| sw.interrupt.status
| request_queue.push <uri>
| request_queue.1.push <uri>
| request_queue.2.push <uri>
END
""",
    )

    tn.push_full_alert(str(wav))
    tn.push_voice_alert(str(wav))
    tn.push_cycle(str(wav))
    assert tn.reset_cycle_safely() is True
    tn.flush_alert()

    assert any(cmd.startswith("sw.full_alert.push_b64 ") for cmd in commands)
    assert any(cmd.startswith("sw.voice_alert.push_b64 ") for cmd in commands)
    assert any(cmd.startswith("sw.cycle.push_b64 ") for cmd in commands)
    assert "sw.cycle.reset" in commands
    assert "sw.full_alert.skip" in commands
    assert "sw.voice_alert.skip" in commands


def test_liquidsoap_telnet_publication_sink_is_called_once(tmp_path: Path) -> None:
    wav = tmp_path / "cycle.wav"
    wav.write_bytes(b"RIFFfakeWAVE")
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands = _wire_fake_send(
        tn,
        """
| sw.cycle.push_b64 <base64-uri>
| sw.cycle.push <uri>
| sw.voice_alert.push_b64 <base64-uri>
| sw.voice_alert.push <uri>
| sw.full_alert.push_b64 <base64-uri>
| sw.full_alert.push <uri>
END
""",
    )
    publications: list[tuple[str, int | None, dict[str, str]]] = []
    tn.set_publication_sink(lambda path, request_id, meta: publications.append((path, request_id, meta)))

    tn.push_cycle(str(wav))

    assert len(publications) == 1
    assert publications[0][0] == str(wav)
    assert publications[0][1] is None
    assert publications[0][2]["sw_plane"] == "cycle"
    assert any(command.startswith("sw.cycle.push_b64 ") for command in commands)


def test_liquidsoap_telnet_uses_split_alert_planes(tmp_path: Path) -> None:
    wav = tmp_path / "alert.wav"
    wav.write_bytes(b"RIFFfakeWAVE")
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands = _wire_fake_send(
        tn,
        """
| cycle.push <uri>
| cycle.flush
| voice_alert.push <uri>
| voice_alert.flush
| voice_alert.skip
| full_alert.push <uri>
| full_alert.flush
| full_alert.skip
END
""",
    )

    tn.push_full_alert(str(wav))
    tn.push_voice_alert(str(wav))
    tn.flush_alert()

    assert any(cmd.startswith("full_alert.push ") for cmd in commands)
    assert any(cmd.startswith("voice_alert.push ") for cmd in commands)
    assert "full_alert.flush" in commands
    assert "voice_alert.flush" in commands


def test_liquidsoap_telnet_collapses_to_legacy_alert_plane(tmp_path: Path) -> None:
    wav = tmp_path / "alert.wav"
    wav.write_bytes(b"RIFFfakeWAVE")
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands = _wire_fake_send(
        tn,
        """
| cycle.push <uri>
| cycle.flush
| alert.push <uri>
| alert.flush
| alert.skip
END
""",
    )

    tn.push_full_alert(str(wav))
    tn.push_voice_alert(str(wav))
    tn.flush_alert()

    assert sum(1 for cmd in commands if cmd.startswith("alert.push ")) == 2
    assert commands.count("alert.flush") == 1


def test_liquidsoap_telnet_maps_anonymous_queues_by_declaration_order(tmp_path: Path) -> None:
    wav = tmp_path / "alert.wav"
    wav.write_bytes(b"RIFFfakeWAVE")
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands = _wire_fake_send(
        tn,
        """
| request_queue.push <uri>
| request_queue.flush_and_skip
| request_queue.skip
| request_queue.1.push <uri>
| request_queue.1.flush_and_skip
| request_queue.1.skip
| request_queue.2.push <uri>
| request_queue.2.flush_and_skip
| request_queue.2.skip
END
""",
    )

    tn.push_cycle(str(wav))
    tn.push_voice_alert(str(wav))
    tn.push_full_alert(str(wav))

    assert any(cmd.startswith("request_queue.push ") for cmd in commands)
    assert any(cmd.startswith("request_queue.1.push ") for cmd in commands)
    assert any(cmd.startswith("request_queue.2.push ") for cmd in commands)

    cycle_cmd = next(cmd for cmd in commands if cmd.startswith("request_queue.push "))
    voice_cmd = next(cmd for cmd in commands if cmd.startswith("request_queue.1.push "))
    full_cmd = next(cmd for cmd in commands if cmd.startswith("request_queue.2.push "))
    assert cycle_cmd != voice_cmd != full_cmd


def test_liquidsoap_telnet_detects_aliases_without_exact_usage_suffix(tmp_path: Path) -> None:
    wav = tmp_path / "alert.wav"
    wav.write_bytes(b"RIFFfakeWAVE")
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands = _wire_fake_send(
        tn,
        """
| sw.cycle.push : Push routine cycle audio.
| sw.cycle.skip
| sw.voice_alert.push <request-uri> : Push voice alert audio.
| sw.voice_alert.skip
| sw.full_alert.push <request-uri> : Push full alert audio.
| sw.full_alert.skip
| request_queue.push <uri>
| request_queue.1.push <uri>
| request_queue.2.push <uri>
END
""",
    )

    tn.push_full_alert(str(wav))
    tn.push_voice_alert(str(wav))
    tn.push_cycle(str(wav))

    assert any(cmd.startswith("sw.full_alert.push ") for cmd in commands)
    assert any(cmd.startswith("sw.voice_alert.push ") for cmd in commands)
    assert any(cmd.startswith("sw.cycle.push ") for cmd in commands)
    assert not any(cmd.startswith("request_queue") and ".push " in cmd for cmd in commands)


def test_liquidsoap_telnet_restores_metadata_over_base64_aliases(tmp_path: Path) -> None:
    wav = tmp_path / "alert with spaces.wav"
    wav.write_bytes(b"RIFFfakeWAVE")
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands = _wire_fake_send(
        tn,
        """
| sw.cycle.push <uri>
| sw.cycle.push_b64 <base64-uri>
| sw.cycle.skip
| sw.voice_alert.push <uri>
| sw.voice_alert.push_b64 <base64-uri>
| sw.voice_alert.skip
| sw.full_alert.push <uri>
| sw.full_alert.push_b64 <base64-uri>
| sw.full_alert.skip
END
""",
    )

    tn.push_full_alert(str(wav), meta={"title": "Manual alert with spaces", "album": "Weather information"})

    push_cmd = next(cmd for cmd in commands if cmd.startswith("sw.full_alert.push_b64 "))
    encoded = push_cmd.split(" ", 1)[1]
    decoded = base64.b64decode(encoded).decode("utf-8")
    assert decoded.startswith("annotate:")
    assert 'title="Manual alert with spaces"' in decoded
    assert 'album="Weather information"' in decoded
    assert decoded.endswith("alert%20with%20spaces.wav")


def test_liquidsoap_telnet_old_sw_aliases_keep_bare_uri_fallback(tmp_path: Path) -> None:
    wav = tmp_path / "alert with spaces.wav"
    wav.write_bytes(b"RIFFfakeWAVE")
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands = _wire_fake_send(
        tn,
        """
| sw.cycle.push <uri>
| sw.voice_alert.push <uri>
| sw.full_alert.push <uri>
END
""",
    )

    tn.push_full_alert(str(wav), meta={"title": "Manual alert with spaces"})

    push_cmd = next(cmd for cmd in commands if cmd.startswith("sw.full_alert.push "))
    assert "annotate:" not in push_cmd
    assert push_cmd.startswith("sw.full_alert.push file://")
    assert "%20" in push_cmd


def test_liquidsoap_telnet_keeps_annotations_for_builtin_request_queues(tmp_path: Path) -> None:
    wav = tmp_path / "alert.wav"
    wav.write_bytes(b"RIFFfakeWAVE")
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands = _wire_fake_send(
        tn,
        """
| request_queue.push <uri>
| request_queue.flush_and_skip
END
""",
    )

    tn.push_cycle(str(wav), meta={"title": "Cycle title"})

    push_cmd = next(cmd for cmd in commands if cmd.startswith("request_queue.push "))
    assert "annotate:" in push_cmd
    assert 'title="Cycle title"' in push_cmd


def test_liquidsoap_telnet_flush_cycle_prefers_guarded_alias() -> None:
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands = _wire_fake_send(
        tn,
        """
| sw.cycle.push <uri>
| sw.cycle.skip
| sw.cycle.reset
| sw.voice_alert.push <uri>
| sw.full_alert.push <uri>
END
""",
    )

    tn.flush_cycle()

    assert "sw.cycle.reset" in commands
    assert "sw.cycle.skip" not in commands


def test_liquidsoap_telnet_safe_cycle_reset_has_no_builtin_fallback() -> None:
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands = _wire_fake_send(
        tn,
        """
| cycle.push <uri>
| cycle.flush_and_skip
| voice_alert.push <uri>
| full_alert.push <uri>
END
""",
    )

    assert tn.reset_cycle_safely() is False
    assert "cycle.flush_and_skip" not in commands


def test_liquidsoap_telnet_reads_interrupt_status_alias() -> None:
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands: list[str] = []
    responses = iter(("ACTIVE\nEND\n", "IDLE\nEND\n"))

    def fake_send(command: str, *, read_deadline=None):
        commands.append(command)
        if command == "help":
            return """
| sw.cycle.push <uri>
| sw.cycle.reset
| sw.voice_alert.push <uri>
| sw.full_alert.push <uri>
| sw.interrupt.status
END
"""
        if command == "sw.interrupt.status":
            return next(responses)
        return "OK\nEND\n"

    tn._send = fake_send  # type: ignore[method-assign]

    assert tn.interrupt_active() is True
    assert tn.interrupt_active() is False
    assert commands.count("sw.interrupt.status") == 2


def test_liquidsoap_telnet_reads_full_publication_inventory() -> None:
    tn = LiquidsoapTelnet("127.0.0.1", 1234)
    commands: list[str] = []

    def fake_send(command: str, *, read_deadline=None):
        commands.append(command)
        if command == "help":
            return """
├─ sw.cycle.push <uri>
├─ sw.voice_alert.push <uri>
├─ sw.full_alert.push <uri>
├─ sw.publication.last
├─ sw.publication.epoch
├─ sw.publication.inventory
END
"""
        if command == "sw.publication.epoch":
            return "epoch-1\nEND\n"
        if command == "sw.publication.last":
            return "pub-1\nEND\n"
        if command == "sw.publication.inventory":
            return "7=playing\n8=ready\n9=resolving\nEND\n"
        return "OK\nEND\n"

    object.__setattr__(tn, "_send", fake_send)

    assert tn.publication_snapshot({7, 8, 9}) == {
        "epoch": "epoch-1",
        "last": "pub-1",
        "all": {7, 8, 9},
        "alive": {7, 8, 9},
        "resolving": {9},
        "on_air": {7},
        "statuses": {7: "playing", 8: "ready", 9: "resolving"},
    }
    assert commands[-1] == "sw.publication.inventory"
