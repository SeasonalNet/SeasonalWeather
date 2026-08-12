from __future__ import annotations

import datetime as dt
import logging
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import AppConfig
from ..tts.audio import write_sine_wav, write_silence_wav, concat_wavs
from ..tts.async_bridge import FinalizationEvidence, synthesize_completed_wav_async
from ..tts.tts import TTS

try:
    from ..same.same import SameHeader, chunk_locations, render_same_bursts_wav, render_same_eom_wav
except Exception:  # pragma: no cover
    SameHeader = None  # type: ignore
    chunk_locations = None  # type: ignore
    render_same_bursts_wav = None  # type: ignore
    render_same_eom_wav = None  # type: ignore


log = logging.getLogger("seasonalweather")


def safe_event_code(raw: str | None) -> str:
    if not raw:
        return "SPS"
    s = "".join(ch for ch in str(raw).upper() if ch.isalnum())
    return s[:3] if len(s) >= 3 else "SPS"


def assert_station_wav_format(wav_path: Path, *, sample_rate: int) -> None:
    try:
        with wave.open(str(wav_path), "rb") as wf:
            channels = int(wf.getnchannels())
            sample_width = int(wf.getsampwidth())
            actual_sample_rate = int(wf.getframerate())
    except wave.Error as exc:
        raise ValueError(f"Input WAV is not readable: {exc}") from exc

    expected_rate = int(sample_rate)
    if channels != 2 or sample_width != 2 or actual_sample_rate != expected_rate:
        raise ValueError(
            f"Input WAV must be stereo 16-bit PCM at {expected_rate} Hz; "
            f"got channels={channels}, sample_width={sample_width}, sample_rate={actual_sample_rate}"
        )


class AudioOriginator:
    """Render alert/manual audio products.

    This service owns TTS/WAV/SAME assembly. The orchestrator decides what should
    air; this class decides how the station-ready WAV is built.
    """

    def __init__(
        self,
        *,
        cfg: AppConfig,
        tts: TTS,
        local_tz: ZoneInfo,
        paths: Callable[[], tuple[Path, Path, Path, Path]],
        discord: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.tts = tts
        self.local_tz = local_tz
        self._paths = paths
        self.discord = discord

    def assert_station_wav_format(self, wav_path: Path) -> None:
        assert_station_wav_format(wav_path, sample_rate=self.cfg.audio.sample_rate)

    async def render_voice_only_audio(self, script_text: str, *, prefix: str = "capvoice") -> Path:
        _, audio_dir, _, _ = self._paths()
        ts = dt.datetime.now(tz=self.local_tz).strftime("%Y%m%d-%H%M%S")
        safe_prefix = "".join(ch for ch in prefix if ch.isalnum() or ch in {"_", "-"}).strip() or "voice"

        out = audio_dir / f"{safe_prefix}_{ts}.wav"

        def finalize(tts_path, cancellation, fence) -> FinalizationEvidence:
            del fence
            staging = tts_path.parent
            pre = staging / "voice-pre.wav"
            post = staging / "voice-post.wav"
            staged = staging / "voice-completed.wav"
            write_silence_wav(post, 1.2, self.cfg.audio.sample_rate)
            write_silence_wav(pre, 0.35, self.cfg.audio.sample_rate)
            if cancellation.is_set():
                raise RuntimeError("voice finalization cancelled")
            concat_wavs(staged, [pre, tts_path, post])
            return FinalizationEvidence(staged)

        await synthesize_completed_wav_async(
            self.tts,
            script_text,
            out,
            purpose="administrative",
            finalize=finalize,
        )
        return out

    async def render_alert_audio(
        self,
        parsed: Any,
        script_text: str,
        *,
        same_locations: list[str] | None = None,
    ) -> Path:
        _, audio_dir, _, _ = self._paths()
        ts = dt.datetime.now(tz=self.local_tz).strftime("%Y%m%d-%H%M%S")

        out = audio_dir / f"alert_{ts}.wav"

        def finalize(tts_path, cancellation, fence) -> FinalizationEvidence:
            del fence
            staging = tts_path.parent
            tone = staging / "alert-tone.wav"
            gap = staging / "alert-gap.wav"
            eom = staging / "alert-eom.wav"
            post = staging / "alert-post.wav"
            staged = staging / "alert-completed.wav"
            same_hdr_all, same_eom_wav = self._render_same_assets(
                audio_dir=staging,
                ts=ts,
                event_code=safe_event_code(getattr(parsed, "product_type", None)),
                same_locations=same_locations,
                log_disabled_message="SAME targeting disabled for this alert (no locations computed)",
                log_success=True,
                failure_context={
                    "alert_type": getattr(parsed, "product_type", None),
                    "wfo": getattr(parsed, "wfo", None),
                },
            )
            write_sine_wav(
                tone,
                self.cfg.audio.attention_tone_hz,
                self.cfg.audio.attention_tone_seconds,
                self.cfg.audio.sample_rate,
            )
            write_silence_wav(gap, self.cfg.audio.inter_segment_silence_seconds, self.cfg.audio.sample_rate)
            write_silence_wav(post, self.cfg.audio.post_alert_silence_seconds, self.cfg.audio.sample_rate)
            parts: list[Path] = []
            if same_hdr_all:
                parts.extend([same_hdr_all, gap])
            parts.extend([tone, gap, tts_path])
            if same_eom_wav:
                parts.extend([gap, same_eom_wav])
            else:
                write_sine_wav(
                    eom,
                    self.cfg.audio.eom_beep_hz,
                    self.cfg.audio.eom_beep_seconds,
                    self.cfg.audio.sample_rate,
                    amplitude=0.18,
                )
                parts.extend([gap, eom])
            parts.append(post)
            if cancellation.is_set():
                raise RuntimeError("alert finalization cancelled")
            concat_wavs(staged, parts)
            return FinalizationEvidence(staged)

        await synthesize_completed_wav_async(
            self.tts,
            script_text,
            out,
            purpose="alert",
            finalize=finalize,
        )
        return out

    async def render_pre_recorded_alert_audio(
        self,
        *,
        event_code: str,
        source_wav: Path,
        same_locations: list[str] | None = None,
    ) -> Path:
        self.assert_station_wav_format(source_wav)
        _, audio_dir, _, _ = self._paths()
        ts = dt.datetime.now(tz=self.local_tz).strftime("%Y%m%d-%H%M%S")

        tone = audio_dir / f"api_audio_alert_{ts}_tone.wav"
        gap = audio_dir / f"api_audio_alert_{ts}_gap.wav"
        eom = audio_dir / f"api_audio_alert_{ts}_eom.wav"
        post = audio_dir / f"api_audio_alert_{ts}_post.wav"
        out = audio_dir / f"api_audio_alert_{ts}.wav"

        same_hdr_all, same_eom_wav = self._render_same_assets(
            audio_dir=audio_dir,
            ts=ts,
            event_code=safe_event_code(event_code),
            same_locations=same_locations,
            prefix="api_audio_alert",
            log_disabled_message="SAME targeting disabled for this prerecorded alert (no locations computed)",
            failure_log_message="SAME generation failed; continuing without SAME for prerecorded manual alert",
        )

        write_sine_wav(
            tone, self.cfg.audio.attention_tone_hz, self.cfg.audio.attention_tone_seconds, self.cfg.audio.sample_rate
        )
        write_silence_wav(gap, self.cfg.audio.inter_segment_silence_seconds, self.cfg.audio.sample_rate)
        write_silence_wav(post, self.cfg.audio.post_alert_silence_seconds, self.cfg.audio.sample_rate)

        parts: list[Path] = []
        if same_hdr_all:
            parts.extend([same_hdr_all, gap])
        parts.extend([tone, gap, source_wav])
        if same_eom_wav:
            parts.extend([gap, same_eom_wav])
        else:
            write_sine_wav(
                eom,
                self.cfg.audio.eom_beep_hz,
                self.cfg.audio.eom_beep_seconds,
                self.cfg.audio.sample_rate,
                amplitude=0.18,
            )
            parts.extend([gap, eom])
        parts.append(post)

        concat_wavs(out, parts)
        return out

    def _render_same_assets(
        self,
        *,
        audio_dir: Path,
        ts: str,
        event_code: str,
        same_locations: list[str] | None,
        prefix: str = "alert",
        log_disabled_message: str,
        failure_log_message: str = "SAME generation failed; continuing without SAME for this alert",
        log_success: bool = False,
        failure_context: dict[str, Any] | None = None,
    ) -> tuple[Path | None, Path | None]:
        same_hdr_all: Path | None = None
        same_eom_wav: Path | None = None

        if not self.cfg.same.enabled or SameHeader is None:
            return None, None

        try:
            # same_locations is None => default to full service area.
            # same_locations is []   => explicitly disable SAME for this alert.
            if same_locations is not None and len(same_locations) == 0:
                log.info(log_disabled_message)
                return None, None

            if same_locations is not None:
                locs = list(same_locations)
            else:
                locs = list(self.cfg.service_area.same_fips_all)

            if not locs:
                locs = ["000000"]

            chunks = chunk_locations(locs) if chunk_locations is not None else [[]]
            issued = dt.datetime.now(tz=dt.timezone.utc)

            hdr_wavs: list[Path] = []
            for i, loc_chunk in enumerate(chunks):
                hdr_msg = SameHeader(
                    org="WXR",
                    event=event_code,
                    locations=tuple(loc_chunk) if loc_chunk else tuple(["000000"]),
                    duration_minutes=self.cfg.same.duration_minutes,
                    sender=self.cfg.same.sender,
                    issued_utc=issued,
                ).as_ascii()

                hw = audio_dir / f"{prefix}_{ts}_samehdr_{i}.wav"
                render_same_bursts_wav(  # type: ignore[misc]
                    hw,
                    hdr_msg,
                    sample_rate=self.cfg.audio.sample_rate,
                    amplitude=self.cfg.same.amplitude,
                    native_encoder=self.cfg.same.native_encoder,
                )
                hdr_wavs.append(hw)

            if len(hdr_wavs) == 1:
                same_hdr_all = hdr_wavs[0]
            elif len(hdr_wavs) > 1:
                msg_gap = audio_dir / f"{prefix}_{ts}_samehdr_msg_gap.wav"
                write_silence_wav(msg_gap, 1.0, self.cfg.audio.sample_rate)

                same_hdr_all = audio_dir / f"{prefix}_{ts}_samehdr_all.wav"
                parts2: list[Path] = []
                for i, hw in enumerate(hdr_wavs):
                    parts2.append(hw)
                    if i != len(hdr_wavs) - 1:
                        parts2.append(msg_gap)
                concat_wavs(same_hdr_all, parts2)

            same_eom_wav = audio_dir / f"{prefix}_{ts}_sameeom.wav"
            render_same_eom_wav(  # type: ignore[misc]
                same_eom_wav,
                sample_rate=self.cfg.audio.sample_rate,
                amplitude=self.cfg.same.amplitude,
                native_encoder=self.cfg.same.native_encoder,
            )

            if log_success:
                log.info(
                    "SAME enabled: event=%s sender=%s locs=%d chunks=%d",
                    event_code,
                    self.cfg.same.sender,
                    len(locs),
                    len(chunks),
                )
        except Exception:
            same_hdr_all = None
            same_eom_wav = None
            log.exception(failure_log_message)
            if self.discord is not None and failure_context is not None:
                self.discord.error(
                    title="SAME generation failed",
                    module="same.py",
                    exception_type="Exception",
                    message="SAME burst rendering raised an exception. Alert aired without SAME headers.",
                    context=failure_context,
                    fallback="Aired voice-only (no SAME)",
                )

        return same_hdr_all, same_eom_wav
