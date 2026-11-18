from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

from .decode import HEADER_RE, SAME_MAX_LOCS

# ffmpeg emits s16le; samedec expects i16 native-endian. On x86/x64 (little-endian), this matches.
BYTES_PER_SAMPLE = 2  # int16 mono
MAX_DECODER_LINE_BYTES = 64 * 1024

# Runtime defaults. The actual values should normally be passed in from ern_gwes.py
# based on config.yaml's samedec: block.
DEFAULT_CONFIDENCE = 0.85
DEFAULT_START_DELAY_S = 1.4
DEFAULT_SAMEDEC_BIN = "/usr/local/bin/samedec"


def _force_line_buffered_stdout() -> None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    os.environ.setdefault("PYTHONUNBUFFERED", "1")


def _is_http_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return u.startswith("http://") or u.startswith("https://")


def _looks_like_local_file(url: str) -> bool:
    u = (url or "").strip()
    if not u:
        return False
    if _is_http_url(u):
        return False
    # treat absolute/relative filesystem paths as file-like if they exist
    try:
        return Path(u).exists()
    except Exception:
        return False


def _spawn_ffmpeg(url: str, sr: int, debug: bool = False) -> subprocess.Popen:
    """
    Spawn ffmpeg that outputs mono s16le PCM to stdout.

    For HTTP streams, add reconnect options to reduce churn.
    """
    stderr = None if debug else subprocess.DEVNULL

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
    ]

    if _is_http_url(url):
        # best-effort: helps a lot on flaky internet radio
        cmd += [
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
        ]

    cmd += [
        "-i",
        url,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(int(sr)),
        "-f",
        "s16le",
        "pipe:1",
    ]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=stderr,
        bufsize=0,
    )


def _spawn_samedec(bin_path: str, sr: int, debug: bool = False) -> subprocess.Popen:
    """
    Spawn samedec. We keep stdout as a byte stream and parse lines ourselves (non-blocking).
    """
    stderr = None if debug else subprocess.DEVNULL
    return subprocess.Popen(
        [str(bin_path), "-r", str(int(sr))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
        bufsize=0,
    )


def _iter_pcm_chunks(
    proc: subprocess.Popen,
    chunk_bytes: int,
    *,
    idle_timeout_seconds: float = 45.0,
) -> Iterator[bytes]:
    """
    Yields aligned PCM chunks.
    Mimics same_listen.py behavior:
      - returns on EOF
      - raises TimeoutError if stdout goes silent too long
    """
    assert proc.stdout is not None
    fd = proc.stdout.fileno()

    carry = b""
    last_data_at = time.monotonic()

    while True:
        if proc.poll() is not None:
            return

        r, _, _ = select.select([fd], [], [], 1.0)
        if not r:
            if (time.monotonic() - last_data_at) > float(idle_timeout_seconds):
                raise TimeoutError(f"ffmpeg stalled: no PCM bytes for {idle_timeout_seconds}s")
            continue

        data = os.read(fd, chunk_bytes)
        if not data:
            return

        last_data_at = time.monotonic()

        data = carry + data
        rem = len(data) % BYTES_PER_SAMPLE
        if rem:
            carry = data[-rem:]
            data = data[:-rem]
        else:
            carry = b""

        if data:
            yield data


def _parse_same_line(line: str) -> Optional[dict]:
    s = (line or "").strip()
    if not s:
        return None

    if s == "NNNN":
        return {"kind": "eom", "text": "NNNN"}

    if s.startswith("ZCZC-") and s.endswith("-"):
        m = HEADER_RE.match(s)
        if not m:
            # Should be rare; still pass through.
            return {"kind": "header", "text": s}

        locs_raw = m.group("locs") or ""
        locs = locs_raw.split("-")[:SAME_MAX_LOCS] if locs_raw else []
        sender = (m.group("sender") or "").rstrip()

        return {
            "kind": "header",
            "text": s,
            "org": m.group("org"),
            "event": m.group("event"),
            "locations": locs,
            "tttt": m.group("tttt"),
            "jjjhhmm": m.group("jjjhhmm"),
            "sender": sender,
        }

    return None


def _emit(obj: dict, jsonl: bool) -> None:
    if jsonl:
        print(json.dumps(obj, ensure_ascii=False), flush=True)
    else:
        kind = obj.get("kind", "?")
        txt = obj.get("text", "")
        conf = float(obj.get("confidence") or 0.0)
        print(f"{kind} conf={conf:.3f} {txt}", flush=True)


def _drain_samedec_stdout(
    sd_fd: int,
    out_buf: bytes | None,
    *,
    sr: int,
    url: str,
    total_samples: int,
    start_delay_s: float,
    confidence: float,
    dedupe_seconds: float,
    last_emit_at: dict[tuple[str, str], float],
    jsonl: bool,
    emitted_counter: list[int],
) -> bytes | None:
    """
    Non-blocking drain of samedec stdout. Updates out_buf and emits any complete lines.
    emitted_counter is a 1-element list so we can mutate it.
    """
    lines: list[bytes] = []
    for _ in range(32):
        r, _, _ = select.select([sd_fd], [], [], 0.0)
        if not r:
            break
        try:
            chunk = os.read(sd_fd, 4096)
        except BlockingIOError:
            break
        if not chunk:
            if lines:
                break
            raise RuntimeError("samedec stdout closed")

        if out_buf is None:
            _, newline, chunk = chunk.partition(b"\n")
            if not newline:
                continue
            out_buf = b""
        parts = (out_buf + chunk).split(b"\n")
        remainder = parts.pop()
        lines.extend(parts)
        out_buf = remainder if len(remainder) <= MAX_DECODER_LINE_BYTES else None

    for line_b in lines:
        if len(line_b) > MAX_DECODER_LINE_BYTES:
            continue
        line_b = line_b.strip()
        if not line_b:
            continue

        line = line_b.decode("utf-8", "replace")
        msg = _parse_same_line(line)
        if not msg:
            continue

        kind = str(msg.get("kind") or "?")
        text = str(msg.get("text") or "")
        key = (kind, text)

        now = time.time()
        last = last_emit_at.get(key, 0.0)
        if (now - last) < float(dedupe_seconds):
            continue

        approx_t = max(0.0, (float(total_samples) / float(sr)) - float(start_delay_s))

        msg.update(
            {
                "confidence": float(confidence),
                "start_seconds": float(approx_t),
                "url": url,
                "sr": int(sr),
                "backend": "samedec",
                "stream_sample_index": int(total_samples),
            }
        )

        try:
            _emit(msg, jsonl=jsonl)
        except BrokenPipeError:
            # stdout consumer went away (e.g. piped to head). Exit cleanly.
            raise SystemExit(0)

        last_emit_at[key] = now
        emitted_counter[0] += 1

    return out_buf


def listen_stream(
    url: str,
    sr: int = 48000,
    tail_seconds: float = 10.0,  # compatibility; samedec path does not need it
    dedupe_seconds: float = 20.0,
    trigger_ratio: float = 8.0,  # compatibility; samedec path does not need it
    jsonl: bool = False,
    debug: bool = False,
    idle_bytes_seconds: float = 45.0,
    *,
    once: bool = False,
    exit_on_eof: Optional[bool] = None,
    start_delay_s: float = DEFAULT_START_DELAY_S,
    confidence: float = DEFAULT_CONFIDENCE,
    samedec_bin: str = DEFAULT_SAMEDEC_BIN,
) -> None:
    """
    Stream listener that decodes SAME using Rust samedec.

    EOF/idle protection is modeled after same_listen.py:
      - idle timeout triggers restart
      - EOF triggers restart for HTTP streams
      - EOF exits for local file inputs (auto), unless overridden

    once: exit after first emitted message (useful for tests)
    exit_on_eof: if None, auto (True for local file path, False for http(s))
    """
    _ = tail_seconds
    _ = trigger_ratio

    if exit_on_eof is None:
        exit_on_eof = _looks_like_local_file(url)

    last_emit_at: dict[tuple[str, str], float] = {}

    while True:
        ff: subprocess.Popen | None = None
        sd: subprocess.Popen | None = None
        try:
            ff = _spawn_ffmpeg(url, sr, debug=debug)
            sd = _spawn_samedec(samedec_bin, sr, debug=debug)

            assert ff is not None and sd is not None
            assert sd.stdin is not None
            assert sd.stdout is not None

            sd_fd = sd.stdout.fileno()
            os.set_blocking(sd_fd, False)

            out_buf: bytes | None = b""
            total_samples = 0
            emitted_counter = [0]

            if debug:
                print(f"[same_listen_samedec] ffmpeg started for {url} @ {sr}Hz", file=sys.stderr, flush=True)

            chunk_bytes = 16384

            for chunk in _iter_pcm_chunks(ff, chunk_bytes, idle_timeout_seconds=idle_bytes_seconds):
                if sd.poll() is not None:
                    raise RuntimeError("samedec exited")

                try:
                    sd.stdin.write(chunk)
                    sd.stdin.flush()
                except BrokenPipeError:
                    raise RuntimeError("samedec pipe closed")

                total_samples += len(chunk) // BYTES_PER_SAMPLE

                out_buf = _drain_samedec_stdout(
                    sd_fd,
                    out_buf,
                    sr=sr,
                    url=url,
                    total_samples=total_samples,
                    start_delay_s=start_delay_s,
                    confidence=confidence,
                    dedupe_seconds=dedupe_seconds,
                    last_emit_at=last_emit_at,
                    jsonl=jsonl,
                    emitted_counter=emitted_counter,
                )

                if once and emitted_counter[0] > 0:
                    return

            # ffmpeg ended (EOF) or proc exited
            if debug:
                print("[same_listen_samedec] ffmpeg EOF / stream ended", file=sys.stderr, flush=True)

            # Signal EOF to samedec and drain anything it already decoded
            try:
                sd.stdin.close()
            except Exception:
                pass

            # Drain remaining output briefly (best effort)
            t_end = time.monotonic() + 2.0
            while time.monotonic() < t_end:
                if sd.poll() is not None:
                    break
                out_buf = _drain_samedec_stdout(
                    sd_fd,
                    out_buf,
                    sr=sr,
                    url=url,
                    total_samples=total_samples,
                    start_delay_s=start_delay_s,
                    confidence=confidence,
                    dedupe_seconds=dedupe_seconds,
                    last_emit_at=last_emit_at,
                    jsonl=jsonl,
                    emitted_counter=emitted_counter,
                )
                if once and emitted_counter[0] > 0:
                    return
                time.sleep(0.05)

            if exit_on_eof:
                return

        except KeyboardInterrupt:
            if debug:
                print("[same_listen_samedec] KeyboardInterrupt, exiting.", file=sys.stderr, flush=True)
            return
        except SystemExit:
            # allow clean exit on BrokenPipe to stdout
            return
        except Exception as e:
            print(f"[same_listen_samedec] ERROR: {e!r} (restarting)", file=sys.stderr, flush=True)
        finally:
            for p in (sd, ff):
                if p is None:
                    continue
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    p.wait(timeout=2)
                except Exception:
                    pass

        # Restart backoff (mirrors same_listen.py)
        time.sleep(1.0)


def main() -> None:
    _force_line_buffered_stdout()

    ap = argparse.ArgumentParser(description="Listen to an audio stream and decode SAME via samedec (Rust).")
    ap.add_argument("--url", required=True, help="Stream URL (http(s)://...) or local file path")
    ap.add_argument("--sr", type=int, default=48000, help="Decode sample rate (Hz)")
    ap.add_argument("--tail", type=float, default=10.0, help="(compat) Seconds of audio to retain (ignored)")
    ap.add_argument("--dedupe", type=float, default=20.0, help="Seconds to suppress duplicate (kind,text)")
    ap.add_argument("--trigger-ratio", type=float, default=8.0, help="(compat) Trigger ratio (ignored)")
    ap.add_argument("--jsonl", action="store_true", help="Emit JSON lines (one per decode)")
    ap.add_argument("--debug", action="store_true", help="More stderr logging")
    ap.add_argument(
        "--idle-bytes", type=float, default=45.0, help="Restart if no PCM bytes arrive for this many seconds"
    )
    ap.add_argument(
        "--confidence",
        type=float,
        default=DEFAULT_CONFIDENCE,
        help="Synthetic confidence to attach to decoded SAME events",
    )
    ap.add_argument(
        "--start-delay-s",
        type=float,
        default=DEFAULT_START_DELAY_S,
        help="Backdate the decoded header start time by this many seconds",
    )
    ap.add_argument("--samedec-bin", default=DEFAULT_SAMEDEC_BIN, help="Path to the samedec binary")

    # Extra test helpers (not used by ern_gwes.py)
    ap.add_argument("--once", action="store_true", help="Exit after first decoded message (testing)")
    ap.add_argument(
        "--exit-on-eof",
        action="store_true",
        help="Exit when ffmpeg EOFs (default auto: True for local file paths, False for http(s))",
    )

    args = ap.parse_args()

    listen_stream(
        url=args.url,
        sr=args.sr,
        tail_seconds=args.tail,
        dedupe_seconds=args.dedupe,
        trigger_ratio=args.trigger_ratio,
        jsonl=args.jsonl,
        debug=args.debug,
        idle_bytes_seconds=args.idle_bytes,
        once=bool(args.once),
        exit_on_eof=(True if args.exit_on_eof else None),
        start_delay_s=args.start_delay_s,
        confidence=args.confidence,
        samedec_bin=args.samedec_bin,
    )


if __name__ == "__main__":
    main()
