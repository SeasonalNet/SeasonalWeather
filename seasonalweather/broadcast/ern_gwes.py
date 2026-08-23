from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from ..diagnostics.bindings import FOUNDATION_CODES
from ..same.locations import normalize_same_allow_set, same_locations_intersect_service_area


log = logging.getLogger("seasonalweather")


@dataclass(frozen=True)
class ErnSameEvent:
    """
    Event emitted when SAME is decoded from an ERN/JON (or similar) node stream.
    This is intentionally narrow: we only carry decoded SAME header/EOM.
    """
    kind: str  # "header" or "eom"
    text: str
    confidence: float
    start_seconds: float

    org: Optional[str] = None
    event: Optional[str] = None
    locations: Tuple[str, ...] = ()
    tttt: Optional[str] = None
    jjjhhmm: Optional[str] = None
    sender: Optional[str] = None

    source: str = "ERN"
    url: str = ""


def _project_root() -> Path:
    # /opt/seasonalweather/app/seasonalweather/broadcast/ern_gwes.py -> parents[2] == /opt/seasonalweather/app
    return Path(__file__).resolve().parents[2]


def _cfg_samedec_args() -> tuple[str, float, float]:
    """
    Read samedec subprocess settings from the already-loaded AppConfig.

    Late-import main._APP_CFG to avoid circular imports at module import time.
    Falls back to sane defaults if the app config is not available.
    """
    try:
        from ..main import _APP_CFG  # late import to avoid circular dependency
    except Exception:
        _APP_CFG = None

    if _APP_CFG is None:
        return "/usr/local/bin/samedec", 0.85, 1.4

    cfg = getattr(_APP_CFG, "samedec", None)
    if cfg is None:
        return "/usr/local/bin/samedec", 0.85, 1.4

    bin_path = str(getattr(cfg, "bin", "/usr/local/bin/samedec") or "/usr/local/bin/samedec").strip()
    confidence = float(getattr(cfg, "confidence", 0.85) or 0.85)
    start_delay_s = float(getattr(cfg, "start_delay_s", 1.4) or 1.4)
    return bin_path, confidence, start_delay_s


def _normalize_decoder_backend(value: str) -> str:
    raw = str(value or "auto").strip().lower().replace("-", "_")
    if raw in {"auto", "default"}:
        return "auto"
    if raw in {"native", "python", "legacy", "internal"}:
        return "native"
    return "samedec"


def _samedec_available(bin_path: str) -> bool:
    candidate = str(bin_path or "").strip()
    if not candidate:
        return False
    if "/" not in candidate:
        return shutil.which(candidate) is not None
    path = Path(candidate)
    return path.exists() and os.access(path, os.X_OK)


def _resolve_decoder_backend(value: str, samedec_bin: str) -> str:
    backend = _normalize_decoder_backend(value)
    if backend == "auto":
        return "samedec" if _samedec_available(samedec_bin) else "native"
    return backend


def _same_listen_module_cmd(
    url: str,
    *,
    sr: int,
    dedupe: float,
    trigger_ratio: float,
    tail: float,
    decoder_backend: str,
    samedec_bin: str,
    samedec_confidence: float,
    samedec_start_delay_s: float,
) -> list[str]:
    backend = _resolve_decoder_backend(decoder_backend, samedec_bin)
    module = "seasonalweather.same.listen_samedec" if backend == "samedec" else "seasonalweather.same.listen"

    cmd = [
        sys.executable,
        "-m",
        module,
        "--url",
        url,
        "--sr",
        str(int(sr)),
        "--dedupe",
        str(float(dedupe)),
        "--trigger-ratio",
        str(float(trigger_ratio)),
        "--tail",
        str(float(tail)),
        "--jsonl",
    ]

    if backend == "samedec":
        cmd.extend(
            [
                "--samedec-bin",
                str(samedec_bin),
                "--confidence",
                str(float(samedec_confidence)),
                "--start-delay-s",
                str(float(samedec_start_delay_s)),
            ]
        )

    return cmd


class ErnGwesMonitor:
    """
    Spawns a configured SAME decoder module, reads JSONL decoded SAME messages,
    filters to service area SAME/FIPS, and emits ErnSameEvent into an asyncio queue.

    This is a "Level 3" source: we do not try to fetch or synthesize official text.
    We just observe and surface SAME activity.
    """

    def __init__(
        self,
        *,
        out_queue: "asyncio.Queue[ErnSameEvent]",
        same_fips_allow: Sequence[str],
        url: str,
        sample_rate: int = 48000,
        dedupe_seconds: float = 20.0,
        trigger_ratio: float = 8.0,
        tail_seconds: float = 10.0,
        confidence_min: float = 0.25,
        name: str = "ERN/JON",
        decoder_backend: str = "auto",
        diagnostic_sink: object | None = None,
    ) -> None:
        self.out_queue = out_queue
        self.same_fips_allow = normalize_same_allow_set(same_fips_allow)
        self.url = str(url).strip()
        self.sample_rate = int(sample_rate)
        self.dedupe_seconds = float(dedupe_seconds)
        self.trigger_ratio = float(trigger_ratio)
        self.tail_seconds = float(tail_seconds)
        self.confidence_min = float(confidence_min)
        self.name = str(name)
        self.decoder_backend = _normalize_decoder_backend(decoder_backend)
        self._diagnostic_sink = diagnostic_sink

        if not self.url:
            raise ValueError("ERN monitor requires a non-empty url")

    def _diagnose(self, code: str, message: str, exception: BaseException | None = None) -> None:
        sink = self._diagnostic_sink
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            return
        emit(
            code,
            component="ern-monitor",
            message=message,
            operational_effect="ERN continuous audio or SAME decoding is degraded within its bounded monitor policy.",
            recovery_action="Inspect the decoder process and retain the existing alert-source fallback behavior.",
            exception=exception,
            source_id=self.name[:128],
        )

    def _service_area_hit(self, locs: Sequence[str]) -> bool:
        if not self.same_fips_allow:
            return True  # if you ever run without config, don't hard-drop everything
        return same_locations_intersect_service_area(locs, self.same_fips_allow)

    def _parse_jsonl_line(self, line: str) -> Optional[ErnSameEvent]:
        try:
            obj = json.loads(line)
        except Exception:
            return None

        kind = str(obj.get("kind") or "").strip().lower()
        text = str(obj.get("text") or "").strip()
        if kind not in {"header", "eom"} or not text:
            return None

        conf = float(obj.get("confidence") or 0.0)
        start_seconds = float(obj.get("start_seconds") or 0.0)

        org = obj.get("org")
        event = obj.get("event")
        tttt = obj.get("tttt")
        jjjhhmm = obj.get("jjjhhmm")
        sender = obj.get("sender")
        locs = obj.get("locations") or []
        try:
            locs_t = tuple(str(x) for x in locs if str(x))
        except Exception:
            locs_t = ()

        return ErnSameEvent(
            kind=kind,
            text=text,
            confidence=conf,
            start_seconds=start_seconds,
            org=str(org) if org else None,
            event=str(event) if event else None,
            locations=locs_t,
            tttt=str(tttt) if tttt else None,
            jjjhhmm=str(jjjhhmm) if jjjhhmm else None,
            sender=str(sender) if sender else None,
            source=self.name,
            url=self.url,
        )

    async def run_forever(self) -> None:
        root = _project_root()

        # Make module import robust under systemd by pinning cwd + PYTHONPATH.
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(root)

        samedec_bin, samedec_confidence, samedec_start_delay_s = _cfg_samedec_args()
        active_decoder_backend = _resolve_decoder_backend(self.decoder_backend, samedec_bin)
        if self.decoder_backend == "auto" and active_decoder_backend == "native":
            log.warning(
                "ERN decoder_backend=auto: samedec binary %s is unavailable; falling back to native decoder",
                samedec_bin,
            )

        cmd = _same_listen_module_cmd(
            self.url,
            sr=self.sample_rate,
            dedupe=self.dedupe_seconds,
            trigger_ratio=self.trigger_ratio,
            tail=self.tail_seconds,
            decoder_backend=active_decoder_backend,
            samedec_bin=samedec_bin,
            samedec_confidence=samedec_confidence,
            samedec_start_delay_s=samedec_start_delay_s,
        )

        log.info("ERN monitor starting (%s backend=%s): %s", self.name, active_decoder_backend, " ".join(cmd))

        while True:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(root),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._diagnose(
                    FOUNDATION_CODES["ern.transport_failed"],
                    "The ERN decoder process could not be started.",
                    exc,
                )
                await asyncio.sleep(2.0)
                continue

            assert proc.stdout is not None
            assert proc.stderr is not None
            stdout = proc.stdout
            stderr = proc.stderr

            async def _drain_stderr() -> None:
                try:
                    while True:
                        b = await stderr.readline()
                        if not b:
                            return
                        s = b.decode("utf-8", "replace").rstrip()
                        if s:
                            log.warning("ERN same_listen stderr: %s", s)
                except Exception:
                    return

            stderr_task = asyncio.create_task(_drain_stderr(), name="ern_same_listen_stderr")

            try:
                while True:
                    b = await stdout.readline()
                    if not b:
                        break
                    line = b.decode("utf-8", "replace").strip()
                    if not line:
                        continue

                    ev = self._parse_jsonl_line(line)
                    if not ev:
                        continue

                    # Confidence gate (keep it low; ERN audio quality can be… "heritage")
                    if ev.confidence < self.confidence_min:
                        continue

                    # Only headers are service-area-filtered; EOM is informational.
                    # IMPORTANT: log out-of-area headers so it doesn't look like decoding failed.
                    if ev.kind == "header" and not self._service_area_hit(ev.locations):
                        log.info(
                            "ERN SAME header out-of-area (dropped): org=%s event=%s sender=%s conf=%.3f same=%s text=%s",
                            ev.org,
                            ev.event,
                            (ev.sender or "").strip(),
                            ev.confidence,
                            ",".join(ev.locations[:12]) + ("..." if len(ev.locations) > 12 else ""),
                            ev.text,
                        )
                        continue

                    # Non-blocking enqueue (drop if full rather than wedging the monitor)
                    try:
                        self.out_queue.put_nowait(ev)
                    except asyncio.QueueFull:
                        log.warning("ERN queue full; dropping event %s %s", ev.kind, ev.text[:32])
                        self._diagnose(
                            FOUNDATION_CODES["ern.stream_degraded"],
                            "The ERN event queue reached its bounded capacity.",
                        )

            finally:
                stderr_task.cancel()
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                with suppress(asyncio.CancelledError):
                    await stderr_task
                with suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)

            # Restart backoff
            log.warning("ERN monitor exited; restarting in 2s (%s)", self.name)
            self._diagnose(
                FOUNDATION_CODES["ern.stream_degraded"],
                "The ERN decoder stream exited and is restarting within its bounded policy.",
            )
            await asyncio.sleep(2.0)
