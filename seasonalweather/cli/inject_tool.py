# =========================================================================================
#      MP"""""`MM                                                       dP              MM'"""'YMM
#      M  mmmmm..M                                                       88              M' .mmm. `M
#      M.      `YM .d8888b. .d8888b. .d8888b. .d8888b. 88d888b. .d8888b. 88              M  MMMMMooM dP    dP 88d888b. 88d888b. .d8888b. 88d888b. .d8888b. dP    dP
#      MMMMMMM.  M 88ooood8 88'  `88 Y8ooooo. 88'  `88 88'  `88 88'  `88 88              M  MMMMMMMM 88    88 88'  `88 88'  `88 88ooood8 88'  `88 88'  `"" 88    88
#      M. .MMM'  M 88.  ... 88.  .88       88 88.  .88 88    88 88.  .88 88              M. `MMM' .M 88.  .88 88       88       88.  ... 88    88 88.  ... 88.  .88
#      Mb.     .dM `88888P' `88888P8 `88888P' `88888P' dP    dP `88888P8 dP              MM.     .dM `88888P' dP       dP       `88888P' dP    dP `88888P' `8888P88
#      MMMMMMMMMMM                                                Seasonal_Currency      MMMMMMMMMMM                                                            .88
#                                                                                                                                                           d8888P.
# =========================================================================================

import argparse
import datetime as dt
import os
import pathlib
import re
import shutil
import subprocess
import sys
import wave
from array import array
from typing import Iterable, Optional

from seasonalweather.config import load_config
from seasonalweather.liquidsoap_telnet import LiquidsoapTelnet
from seasonalweather.tts.tts import TTS
from seasonalweather.alerts.product import parse_product_text
from seasonalweather.alerts.builder import build_spoken_alert
from seasonalweather.same.same import SameHeader, chunk_locations, render_same_bursts_wav, render_same_eom_wav


DEFAULT_CONFIG = "/etc/seasonalweather/config.yaml"


def _now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _chmod_readable(p: pathlib.Path) -> None:
    try:
        p.chmod(0o644)
    except Exception:
        pass


def _write_stereo_pcm16_wav(path: pathlib.Path, pcm_mono: array, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))

        out = array("h")
        for s in pcm_mono:
            out.append(s)
            out.append(s)
        wf.writeframes(out.tobytes())


def _write_silence_wav(path: pathlib.Path, seconds: float, sample_rate: int) -> None:
    n = int(max(0.0, float(seconds)) * int(sample_rate))
    pcm = array("h", [0] * n)
    _write_stereo_pcm16_wav(path, pcm, sample_rate)


def _write_sine_wav(
    path: pathlib.Path, freq_hz: float, seconds: float, sample_rate: int, amplitude: float = 0.22
) -> None:
    sr = int(sample_rate)
    secs = max(0.0, float(seconds))
    amp = max(0.0, min(1.0, float(amplitude)))
    peak = int(32767 * amp)

    n = int(sr * secs)
    pcm = array("h")
    two_pi = 2.0 * 3.141592653589793
    for i in range(n):
        t = i / sr
        v = peak * __import__("math").sin(two_pi * float(freq_hz) * t)
        pcm.append(int(v))
    _write_stereo_pcm16_wav(path, pcm, sr)


def _wav_info(path: pathlib.Path) -> tuple[int, int, int]:
    """return (sr, channels, sampwidth_bytes)"""
    with wave.open(str(path), "rb") as wf:
        return wf.getframerate(), wf.getnchannels(), wf.getsampwidth()


def _ffmpeg_convert(in_wav: pathlib.Path, out_wav: pathlib.Path, sr: int) -> None:
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg not found, but a WAV needed conversion (install ffmpeg or feed PCM16 WAVs).")
    cmd = [
        ff,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(in_wav),
        "-ac",
        "2",
        "-ar",
        str(sr),
        "-sample_fmt",
        "s16",
        str(out_wav),
    ]
    subprocess.check_call(cmd)


def _ensure_pcm16_stereo_sr(path: pathlib.Path, sr: int) -> pathlib.Path:
    """Ensure file is PCM16 stereo at sr. Convert via ffmpeg if not."""
    try:
        fr, ch, sw = _wav_info(path)
        if fr == sr and ch == 2 and sw == 2:
            return path
    except Exception:
        pass

    out = path.with_name(path.stem + f"_norm_{sr}.wav")
    _ffmpeg_convert(path, out, sr)
    _chmod_readable(out)
    return out


def _concat_wavs(out_wav: pathlib.Path, inputs: list[pathlib.Path], sr: int) -> None:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(int(sr))

        for p in inputs:
            with wave.open(str(p), "rb") as wf:
                # stream in chunks
                while True:
                    frames = wf.readframes(8192)
                    if not frames:
                        break
                    out.writeframes(frames)
    _chmod_readable(out_wav)


def _safety_check_text(text: str, *, max_chars: int, allow_unsafe: bool, force_long: bool) -> None:
    t = (text or "").strip()
    if not t:
        raise SystemExit("Refusing empty text.")

    if not force_long and len(t) > max_chars:
        raise SystemExit(
            f"Refusing: message is {len(t)} chars (cap {max_chars}). Use --force-long if you REALLY mean it."
        )

    # crude “I pasted a curse-loop” detector
    words = re.findall(r"[A-Za-z0-9']+", t.lower())
    if not force_long and len(words) >= 120:
        uniq = len(set(words))
        ratio = uniq / max(1, len(words))
        if ratio < 0.22:
            raise SystemExit(
                f"Refusing: looks like heavy repetition (unique/total={ratio:.2f}). Use --force-long to override."
            )

    if not allow_unsafe:
        banned = {"fuck", "shit"}  # minimal, on purpose
        if any(w in banned for w in words):
            raise SystemExit(
                "Refusing: message contains banned words (default safety). "
                "Use --allow-unsafe if you really want to blast that into the stream."
            )


def _telnet(host: str, port: int) -> LiquidsoapTelnet:
    return LiquidsoapTelnet(host=host, port=int(port))


def _push_alert(
    wav_path: pathlib.Path,
    *,
    host: str,
    port: int,
    flush_alert: bool,
    flush_cycle: bool,
) -> None:
    tn = _telnet(host, port)

    # best-effort flushes (depends on your telnet API)
    if flush_alert and hasattr(tn, "flush_alert"):
        try:
            tn.flush_alert()
        except Exception:
            pass

    if flush_cycle and hasattr(tn, "flush_cycle"):
        try:
            tn.flush_cycle()
        except Exception:
            pass

    if hasattr(tn, "push_alert"):
        tn.push_alert(str(wav_path))
        return

    raise RuntimeError("LiquidsoapTelnet object has no push_alert(). Update method name in this script.")


def _build_same_header(
    *,
    org: str,
    event: str,
    locs: list[str],
    duration_min: int,
    sender: str,
) -> str:
    issued = dt.datetime.now(dt.timezone.utc)
    loc_chunks = chunk_locations(locs)
    # For now we only use the first chunk (<=31 locs). You can extend later.
    header = SameHeader(
        org=org,
        event=event,
        locations=tuple(loc_chunks[0] if loc_chunks else []),
        duration_minutes=int(duration_min),
        sender=sender,
        issued_utc=issued,
    )
    return header.as_ascii()


def _render_alert_block_wav(
    *,
    cfg_path: str,
    spoken_text: str,
    org: str,
    event: str,
    locs: list[str],
    duration_min: int,
    sender: str,
    tone_seconds: float,
    same_amp: float,
    same_pause: float,
    gap_seconds: float,
    post_seconds: float,
) -> pathlib.Path:
    cfg = load_config(cfg_path)
    sr = int(cfg.audio.sample_rate)
    output_dir = pathlib.Path(cfg.paths.temporary_dir) / "seasonalweather-inject-audio"
    output_dir.mkdir(parents=True, exist_ok=True)

    base = f"inject_{_now_stamp()}"
    same_hdr = output_dir / f"{base}_same_header.wav"
    same_eom = output_dir / f"{base}_same_eom.wav"
    gap = output_dir / f"{base}_gap.wav"
    tone = output_dir / f"{base}_1050.wav"
    voice = output_dir / f"{base}_voice.wav"
    out = output_dir / f"{base}.wav"

    _write_silence_wav(gap, gap_seconds, sr)
    _write_sine_wav(tone, 1050.0, tone_seconds, sr, amplitude=0.22)

    header_ascii = _build_same_header(org=org, event=event, locs=locs, duration_min=duration_min, sender=sender)
    render_same_bursts_wav(
        same_hdr,
        header_ascii,
        sample_rate=sr,
        amplitude=float(same_amp),
        burst_count=3,
        inter_burst_pause_seconds=float(same_pause),
        native_encoder=cfg.same.native_encoder,
    )
    render_same_eom_wav(
        same_eom,
        sample_rate=sr,
        amplitude=float(same_amp),
        burst_count=3,
        inter_burst_pause_seconds=float(same_pause),
        native_encoder=cfg.same.native_encoder,
    )

    tts = TTS(
        backend=cfg.tts.backend,
        local_engine=cfg.tts.local.engine,
        voice=cfg.tts.voice,
        rate_wpm=cfg.tts.rate_wpm,
        volume=cfg.tts.volume,
        sample_rate=sr,
        text_overrides=cfg.tts.text_overrides,
        vtp_cfg=cfg.tts.voicetext_paul,
        fallback_backend=cfg.tts.fallback_backend,
        tts_data_base=cfg.paths.operational_state_dir,
    )
    tts.synth_to_wav(spoken_text, voice, purpose="administrative")

    # Normalize everything to PCM16 stereo at sr
    parts = [
        _ensure_pcm16_stereo_sr(same_hdr, sr),
        _ensure_pcm16_stereo_sr(gap, sr),
        _ensure_pcm16_stereo_sr(tone, sr),
        _ensure_pcm16_stereo_sr(gap, sr),
        _ensure_pcm16_stereo_sr(voice, sr),
        _ensure_pcm16_stereo_sr(gap, sr),
        _ensure_pcm16_stereo_sr(same_eom, sr),
    ]

    if post_seconds and post_seconds > 0:
        post = output_dir / f"{base}_post.wav"
        _write_silence_wav(post, post_seconds, sr)
        parts.append(_ensure_pcm16_stereo_sr(post, sr))

    _concat_wavs(out, parts, sr)
    return out


def _liquidsoap_endpoint(config_path: str, host: str | None, port: int | None) -> tuple[str, int]:
    network = load_config(config_path).network.liquidsoap
    return host or network.host, int(port if port is not None else network.port)


def cmd_test_alert(args: argparse.Namespace) -> int:
    _safety_check_text(args.text, max_chars=args.max_chars, allow_unsafe=args.allow_unsafe, force_long=args.force_long)

    out = _render_alert_block_wav(
        cfg_path=args.config,
        spoken_text=args.text,
        org=args.org,
        event=args.event,
        locs=args.loc or [],
        duration_min=args.duration_min,
        sender=args.sender,
        tone_seconds=args.tone_seconds,
        same_amp=args.same_amp,
        same_pause=args.same_pause,
        gap_seconds=args.gap_seconds,
        post_seconds=args.post_seconds,
    )

    if not args.dry_run:
        host, port = _liquidsoap_endpoint(args.config, args.telnet_host, args.telnet_port)
        _push_alert(
            out,
            host=host,
            port=port,
            flush_alert=args.flush_alert,
            flush_cycle=args.flush_cycle,
        )

    print(f"OK: built {out}")
    return 0


def cmd_inject_raw(args: argparse.Namespace) -> int:
    raw: str
    if args.file:
        raw = pathlib.Path(args.file).read_text(encoding="utf-8", errors="replace")
    else:
        raw = sys.stdin.read()

    raw = (raw or "").strip()
    if not raw:
        raise SystemExit("No product text provided (use --file or pipe into stdin).")

    parsed = parse_product_text(raw)
    if not parsed:
        raise SystemExit("parse_product_text() returned None (bad/unsupported product format).")

    spoken = build_spoken_alert(parsed, raw)
    text = spoken.script

    _safety_check_text(text, max_chars=args.max_chars, allow_unsafe=args.allow_unsafe, force_long=args.force_long)

    out = _render_alert_block_wav(
        cfg_path=args.config,
        spoken_text=text,
        org=args.org,
        event=args.event,
        locs=args.loc or [],
        duration_min=args.duration_min,
        sender=args.sender,
        tone_seconds=args.tone_seconds,
        same_amp=args.same_amp,
        same_pause=args.same_pause,
        gap_seconds=args.gap_seconds,
        post_seconds=args.post_seconds,
    )

    if not args.dry_run:
        host, port = _liquidsoap_endpoint(args.config, args.telnet_host, args.telnet_port)
        _push_alert(
            out,
            host=host,
            port=port,
            flush_alert=args.flush_alert,
            flush_cycle=args.flush_cycle,
        )

    print(f"OK: built {out}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="seasonalweather-inject", description="Inject SAME+1050Hz+audio into Liquidsoap alert queue"
    )
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="Path to SeasonalWeather config.yaml")
    ap.add_argument("--telnet-host", default=None)
    ap.add_argument("--telnet-port", default=None, type=int)

    ap.add_argument("--flush-alert", action="store_true", help="Flush alert queue before pushing")
    ap.add_argument("--flush-cycle", action="store_true", help="Flush cycle queue (best-effort)")

    ap.add_argument("--dry-run", action="store_true", help="Build WAV but do not push to Liquidsoap")

    # SAME params
    ap.add_argument("--org", default="WXR", help="SAME ORG (3 chars), default WXR")
    ap.add_argument("--event", default="CEM", help="SAME event code (3 chars), default CEM")
    ap.add_argument("--sender", default="SEASNWXR", help="SAME sender (<=8, alnum), default SEASNWXR")
    ap.add_argument("--duration-min", default=30, type=int, help="SAME duration in minutes (rounded to 15m blocks)")
    ap.add_argument("--loc", action="append", help="SAME location PSSCCC (6 digits). Repeatable. If none, uses 000000.")

    # audio timings
    ap.add_argument("--tone-seconds", default=8.0, type=float, help="1050Hz tone length")
    ap.add_argument("--gap-seconds", default=0.6, type=float, help="Silence between segments")
    ap.add_argument("--post-seconds", default=0.8, type=float, help="Silence at the end")

    # SAME render tuning
    ap.add_argument("--same-amp", default=0.35, type=float, help="SAME amplitude (0..1)")
    ap.add_argument("--same-pause", default=1.0, type=float, help="Pause between SAME bursts")

    # safety
    ap.add_argument("--max-chars", default=420, type=int, help="Max chars allowed in spoken text unless --force-long")
    ap.add_argument("--allow-unsafe", action="store_true", help="Allow profanity (default: blocked)")
    ap.add_argument("--force-long", action="store_true", help="Allow long/repetitive text")

    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_test = sub.add_parser("test-alert", help="Speak a literal message as an alert block")
    ap_test.add_argument("text")
    ap_test.set_defaults(func=cmd_test_alert)

    ap_raw = sub.add_parser("inject-raw", help="Parse a raw product text and speak it as an alert block")
    ap_raw.add_argument("--file", help="Read product text from file (else stdin)")
    ap_raw.set_defaults(func=cmd_inject_raw)

    args = ap.parse_args(argv)
    if not args.loc:
        args.loc = ["000000"]
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
