from seasonalweather.broadcast.ern_gwes import _same_listen_module_cmd
from seasonalweather.same import listen_samedec


def test_samedec_decoder_command_uses_samedec_module_and_flags() -> None:
    cmd = _same_listen_module_cmd(
        "http://example.invalid/stream",
        sr=48000,
        dedupe=20.0,
        trigger_ratio=8.0,
        tail=10.0,
        decoder_backend="samedec",
        samedec_bin="/usr/local/bin/samedec",
        samedec_confidence=0.85,
        samedec_start_delay_s=1.4,
    )

    assert "seasonalweather.same.listen_samedec" in cmd
    assert "--samedec-bin" in cmd
    assert "--confidence" in cmd
    assert "--start-delay-s" in cmd


def test_native_decoder_command_omits_samedec_only_flags() -> None:
    cmd = _same_listen_module_cmd(
        "http://example.invalid/stream",
        sr=48000,
        dedupe=20.0,
        trigger_ratio=8.0,
        tail=10.0,
        decoder_backend="native",
        samedec_bin="/usr/local/bin/samedec",
        samedec_confidence=0.85,
        samedec_start_delay_s=1.4,
    )

    assert "seasonalweather.same.listen" in cmd
    assert "seasonalweather.same.listen_samedec" not in cmd
    assert "--samedec-bin" not in cmd
    assert "--confidence" not in cmd
    assert "--start-delay-s" not in cmd


def test_auto_decoder_falls_back_to_native_when_samedec_missing() -> None:
    cmd = _same_listen_module_cmd(
        "http://example.invalid/stream",
        sr=48000,
        dedupe=20.0,
        trigger_ratio=8.0,
        tail=10.0,
        decoder_backend="auto",
        samedec_bin="/definitely/missing/samedec",
        samedec_confidence=0.85,
        samedec_start_delay_s=1.4,
    )

    assert "seasonalweather.same.listen" in cmd
    assert "seasonalweather.same.listen_samedec" not in cmd
    assert "--samedec-bin" not in cmd


def test_samedec_bounds_continuously_readable_pipe_and_discards_line_suffix(monkeypatch) -> None:
    reads = 0
    emitted = []

    def read(_fd: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        if reads == 33:
            return b"discarded suffix\nvalid line\n"
        if reads > 33:
            raise BlockingIOError
        return b"x" * size

    monkeypatch.setattr(listen_samedec.select, "select", lambda *_args: ([1], [], []))
    monkeypatch.setattr(listen_samedec.os, "read", read)
    monkeypatch.setattr(listen_samedec, "_parse_same_line", lambda line: {"kind": "test", "text": line})
    monkeypatch.setattr(listen_samedec, "_emit", lambda message, **_kwargs: emitted.append(message["text"]))

    def drain(buffer: bytes | None) -> bytes | None:
        return listen_samedec._drain_samedec_stdout(
            1,
            buffer,
            sr=48_000,
            url="file:///stream",
            total_samples=0,
            start_delay_s=0.0,
            confidence=1.0,
            dedupe_seconds=20.0,
            last_emit_at={},
            jsonl=True,
            emitted_counter=[0],
        )

    pending = drain(b"")
    assert reads == 32 and pending is None
    assert drain(pending) == b""
    assert emitted == ["valid line"]
