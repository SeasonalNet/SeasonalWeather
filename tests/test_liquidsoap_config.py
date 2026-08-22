from pathlib import Path


def test_radio_liq_uses_flat_priority_fallback_with_compatible_queue_syntax():
    script = Path("liquidsoap/radio.liq").read_text()

    assert 'cycle = request.queue(id="cycle")' in script
    assert 'voice_alert = request.queue(id="voice_alert")' in script
    assert 'full_alert = request.queue(id="full_alert")' in script
    assert "conservative=" not in script
    assert "alerts = fallback" not in script
    assert "radio = fallback(track_sensitive=false, [full_alert, voice_alert, cycle, silence])" in script
    assert 'server.register(namespace="sw.cycle", usage="push <uri>"' in script
    assert 'server.register(namespace="sw.voice_alert", usage="push <uri>"' in script
    assert 'server.register(namespace="sw.full_alert", usage="push <uri>"' in script
    assert 'server.register(namespace="sw.cycle", usage="push_b64 <base64-uri>"' in script
    assert 'server.register(namespace="sw.voice_alert", usage="push_b64 <base64-uri>"' in script
    assert 'server.register(namespace="sw.full_alert", usage="push_b64 <base64-uri>"' in script
    assert 'server.register(namespace="sw.cycle", usage="skip"' in script
    assert 'server.register(namespace="sw.cycle", usage="reset"' in script
    assert 'server.register(namespace="sw.voice_alert", usage="skip"' in script
    assert 'server.register(namespace="sw.full_alert", usage="skip"' in script
    assert 'server.register(namespace="sw.interrupt", usage="status"' in script
    assert 'usage="<uri>"' not in script
    assert 'usage=""' not in script
    assert 'description="Push voice alert audio and nudge cycle playback."' not in script
    assert 'description="Push full alert audio and nudge cycle playback."' not in script
    voice_push = script.split("def sw_voice_alert_push(uri) =", 1)[1].split("end", 1)[0]
    full_push = script.split("def sw_full_alert_push(uri) =", 1)[1].split("end", 1)[0]
    assert "source.skip(cycle)" not in voice_push
    assert "source.skip(cycle)" not in full_push

    cycle_push_b64 = script.split("def sw_cycle_push_b64(payload) =", 1)[1].split("end", 1)[0]
    voice_push_b64 = script.split("def sw_voice_alert_push_b64(payload) =", 1)[1].split("end", 1)[0]
    full_push_b64 = script.split("def sw_full_alert_push_b64(payload) =", 1)[1].split("end", 1)[0]
    assert "request.create(string.base64.decode(payload))" in cycle_push_b64
    assert "request.create(string.base64.decode(payload))" in voice_push_b64
    assert "request.create(string.base64.decode(payload))" in full_push_b64

    cycle_reset = script.split("def sw_cycle_reset(_) =", 1)[1].split("end", 1)[0]
    assert "queued = cycle.queue()" in cycle_reset
    assert "cycle.set_queue([])" in cycle_reset
    assert "list.iter(request.destroy, queued)" in cycle_reset
    assert "if cycle.current() != null() then" in cycle_reset
    assert "cycle.skip()" in cycle_reset


def test_radio_liq_has_container_endpoint_and_secret_overrides():
    script = Path("liquidsoap/radio.liq").read_text()

    assert 'getenv("SEASONALWEATHER_CONTAINER")' in script
    assert "SEASONALWEATHER_LIQUIDSOAP_TELNET_BIND_ADDR" in script
    assert "SEASONALWEATHER_ICECAST_HOST" in script
    assert "SEASONALWEATHER_ICECAST_PORT" in script
    assert "SEASONALWEATHER_ICECAST_PASSWORD_FILE" in script
    assert "file.read(icecast_password_file)" in script
    assert 'password="seasonal-source"' not in script
