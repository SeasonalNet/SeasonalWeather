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

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path

_SPACE_RE = re.compile(r"[ \t]+")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)", re.IGNORECASE)
_ANGLE_URL_RE = re.compile(r"<(https?://[^>]+)>", re.IGNORECASE)
_NWS_COMPACT_CLOCK_RE = re.compile(
    r"(?<![A-Z0-9])(?P<clock>\d{3,4})\s*(?P<ampm>AM|PM)\b",
    re.IGNORECASE,
)

_NWS_MACHINE_BLOCK_START_RE = re.compile(
    r"^(?:"
    r"LAT\.\.\.LON|"
    r"TIME\.\.\.MOT\.\.\.LOC|"
    r"TORNADO\.\.\.|"
    r"TORNADO DAMAGE THREAT\.\.\.|"
    r"THUNDERSTORM DAMAGE THREAT\.\.\.|"
    r"FLASH FLOOD DAMAGE THREAT\.\.\.|"
    r"DAMAGE THREAT\.\.\.|"
    r"HAIL THREAT\.\.\.|"
    r"MAX HAIL SIZE\.\.\.|"
    r"WIND THREAT\.\.\.|"
    r"MAX WIND GUST\.\.\.|"
    r"EXPECTED RAINFALL RATE\.\.\.|"
    r"RAINFALL AMOUNT\.\.\."
    r")",
    re.IGNORECASE,
)
_NWS_COORDINATE_ROW_RE = re.compile(r"^(?:\d{4}\s+){2,}\d{4}\.?$")
_NWS_COORDINATE_TAIL_RE = re.compile(r"(?:\s+\d{4}){4,}\.?$")
_NWS_SAINT_NAME_RE = re.compile(r"\bSt\.\s+(?=[A-Z][A-Za-z'’-]*\b)")
_NWS_SAINT_ALL_CAPS_RE = re.compile(r"\bST\.\s+(?=[A-Z][A-Z'’-]*\b)")

_NWS_TZ_ABBR_RE = re.compile(
    r"\b(EDT|EST|CDT|CST|MDT|MST|PDT|PST|AKDT|AKST|HST|UTC|GMT)\b",
    re.IGNORECASE,
)
_NWS_AMPM_ABBR_RE = re.compile(r"\b(AM|PM)\b", re.IGNORECASE)
_NWS_STATE_ABBRS = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
}
_NWS_STATE_ABBR_RE = re.compile(
    r"\b(" + "|".join(sorted(_NWS_STATE_ABBRS)) + r")\b",
    re.IGNORECASE,
)

# Characters that commonly trail a URL in NWS product text but are not part of it.
_URL_TRAIL_RE = re.compile(r"[.,;:)\]]+$")


def verbalize_url(url: str) -> str:
    """Convert a URL to a spoken form suitable for TTS / NWR broadcast.

    Examples:
        http://dcnr.pa.gov/Communities/Wildfire
            -> "dcnr dot pa dot gov slash communities slash wildfire"
        www.wvforestry.com
            -> "www dot wvforestry dot com"
    """
    u = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    # Strip trailing punctuation that is not part of the URL (e.g. a sentence-ending period).
    u = _URL_TRAIL_RE.sub("", u)
    u = u.replace(".", " dot ")
    u = u.replace("/", " slash ")
    u = u.replace("-", " dash ")
    u = u.replace("_", " ")
    u = u.replace("=", " equals ")
    u = u.replace("?", " ")
    u = u.replace("&", " and ")
    u = re.sub(r"\s+", " ", u).strip().lower()
    return u


def normalize_nws_spoken_times(text: str) -> str:
    """Normalize compact NWS local times like ``700 PM`` to ``7:00 PM``.

    Many NWS products omit the colon in human-readable local times.  Some TTS
    engines read those compact forms as raw numbers (for example, ``700 PM`` as
    "seven hundred PM") instead of clock times.  This helper only touches
    12-hour AM/PM forms and intentionally leaves VTEC/UTC timestamps such as
    ``260513T2300Z`` or ``2251Z`` unchanged.
    """
    if not text:
        return ""

    def _repl(m: re.Match[str]) -> str:
        raw_clock = m.group("clock")
        ampm = m.group("ampm").upper()
        hour = int(raw_clock[:-2])
        minute = int(raw_clock[-2:])

        if hour < 1 or hour > 12 or minute > 59:
            return m.group(0)

        return f"{hour}:{minute:02d} {ampm}"

    return _NWS_COMPACT_CLOCK_RE.sub(_repl, text)


def _compile_text_override_rx(spec: dict) -> re.Pattern[str]:
    match = str(spec.get("match", "") or "")
    if not match:
        raise ValueError("text override is missing 'match'")
    flags = re.IGNORECASE if bool(spec.get("ignore_case", False)) else 0
    if bool(spec.get("regex", False)):
        return re.compile(match, flags)
    return re.compile(re.escape(match), flags)


def _apply_text_overrides(text: str, overrides: list[dict] | None) -> str:
    s = text or ""
    for spec in overrides or []:
        if not isinstance(spec, dict):
            continue
        repl = str(spec.get("replace", "") or "")
        match = str(spec.get("match", "") or "")
        if not match:
            continue
        try:
            rx = _compile_text_override_rx(spec)
        except Exception:
            continue
        s = rx.sub(repl, s)
    return s


# Common “NWS product wrapper” / footer junk we do NOT want spoken
_SKIP_LINE_RE = re.compile(r"^\s*(?:\$\$|&&|NNNN|0{3,})\s*$")

# WMO-style header line (ex: FXUS61 KLWX 201925)
_WMO_HEADER_RE = re.compile(r"^[A-Z]{3,6}\d{2}\s+[A-Z]{4}\s+\d{6}(?:\s+[A-Z]{3})?$")
# Human-readable issued line (ex: 1118 AM EDT Mon Mar 16 2026)
_NWS_ISSUED_LINE_RE = re.compile(r"^\d{3,4}\s*(?:AM|PM)\s+[A-Z]{2,4}\s+[A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{4}$")
_PRODUCT_MASTHEAD_RE = re.compile(
    r"^(?:URGENT\s*-\s*)?(?:WINTER WEATHER MESSAGE|COASTAL HAZARD MESSAGE|SPECIAL WEATHER STATEMENT|"
    r"FLOOD WARNING|FLOOD WATCH|FLOOD ADVISORY|SEVERE WEATHER STATEMENT|SEVERE THUNDERSTORM WARNING|"
    r"TORNADO WARNING|BLIZZARD WARNING|HIGH WIND WARNING|HURRICANE LOCAL STATEMENT|"
    r"REGIONAL WEATHER SUMMARY|HAZARDOUS WEATHER OUTLOOK|AREA FORECAST DISCUSSION|"
    r"ZONE FORECAST PRODUCT|REGIONAL WEATHER SYNOPSIS)\s*$",
    re.IGNORECASE,
)

# A line that is mostly uppercase/digits/punct and short -> often metadata, not prose
_METAISH_RE = re.compile(r"^[A-Z0-9 \-\/\.\(\):;,+#]{1,50}$")

# Stuff that tends to appear in footers
_FOOTER_PREFIXES = (
    "visit us at",
    "for more information",
    "follow us on",
    "facebook",
    "twitter",
    "youtube",
    "weather.gov",
    "national weather service",  # product masthead: e.g. "National Weather Service Baltimore MD/Washington DC"
    "here is a look at the weather features",
)

# HWO section lines like:
#   .DAY ONE...Tonight
#   .DAYS TWO THROUGH SEVEN...Sunday through Friday
#   .SPOTTER INFORMATION STATEMENT...
_HWO_SECTION_LINE_RE = re.compile(r"^\.(?P<title>[^.]+?)\.\.\.(?P<rest>.*)$")

# Zone-code-ish line like: MDZ003>006-503-505-...-212115-
_ZONEISH_CHARS_RE = re.compile(r"^[A-Z0-9>\-.,]+$")


def _looks_like_hwo(text: str) -> bool:
    low = (text or "").lower()
    if "hazardous weather outlook" in low:
        return True
    if re.search(
        r"^\.(day one|days two through seven|spotter information statement)\.\.\.",
        text or "",
        re.IGNORECASE | re.MULTILINE,
    ):
        return True
    return False


def _is_zoneish_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if " " in s:
        return False
    if len(s) < 10:
        return False
    if not _ZONEISH_CHARS_RE.fullmatch(s):
        return False
    if "-" not in s and ">" not in s:
        return False
    if not any(ch.isdigit() for ch in s):
        return False
    # common pattern like MDZ003 / VAZ053 / WVZ050 / ANZ530 etc
    if not re.search(r"[A-Z]{2,4}\d{3}", s):
        return False
    return True


def _looks_like_all_caps_prose(line: str) -> bool:
    """Return true for NWS all-caps prose that TTS must not spell out."""
    s = (line or "").strip()
    letters = re.sub(r"[^A-Za-z]+", "", s)
    if len(letters) < 3 or any(ch.islower() for ch in letters):
        return False
    return bool(re.search(r"\s", s)) or s.startswith("...")


def _sentence_case_nws_prose(line: str) -> str:
    """Sentence-case all-caps NWS prose while preserving key abbreviations."""
    s = (line or "").strip()
    if not _looks_like_all_caps_prose(s):
        return _NWS_SAINT_NAME_RE.sub("Saint ", s)

    s = re.sub(r"^\.\.\.\s*", "", s)
    if s.endswith("..."):
        s = s[:-3].rstrip() + "."

    s = _NWS_SAINT_ALL_CAPS_RE.sub("Saint ", s)
    s = s.lower()
    if s:
        s = s[0].upper() + s[1:]

    s = re.sub(
        r"\bsaint\s+([a-z])",
        lambda m: f"Saint {m.group(1).upper()}",
        s,
    )
    s = _NWS_AMPM_ABBR_RE.sub(lambda m: m.group(1).upper(), s)
    s = _NWS_TZ_ABBR_RE.sub(lambda m: m.group(1).upper(), s)
    s = _NWS_STATE_ABBR_RE.sub(lambda m: m.group(1).upper(), s)
    return s


def clean_for_tts(text: str) -> str:
    """
    De-noise NWS-ish content without rewriting meaning too aggressively.
    This runs on *everything* before it gets spoken, so keep it conservative.
    """
    if not text:
        return ""

    # Normalize newlines first (DECtalk clause/line mode benefits from real line breaks)
    t = text.replace("\r\n", "\n").replace("\r", "\n")

    # HWO needs special handling: it's ALL CAPS + huge zone blocks that should not be spoken.
    is_hwo = _looks_like_hwo(t)
    skip_hwo_zone_block = False
    skip_nws_machine_block = False

    # Strip a few common formatting artifacts
    t = t.replace("*", " ")
    t = t.replace("\u2022", " ")  # bullet
    t = t.replace("\u2013", "-")  # en-dash
    t = t.replace("\u2014", "-")  # em-dash

    # Markdown links: [label](url) -> label
    t = _MD_LINK_RE.sub(r"\1", t)
    # <https://...> -> (remove)
    t = _ANGLE_URL_RE.sub("", t)

    lines_out: list[str] = []
    for raw in t.split("\n"):
        line = raw.strip()
        if not line:
            continue

        # Machine-readable blocks are terminal metadata, not prose.  NWS
        # products normally place ``&&`` before them, but malformed products
        # sometimes omit that delimiter.  Once a block starts, discard all
        # continuation rows through the next product delimiter (or EOF).
        if skip_nws_machine_block:
            if _SKIP_LINE_RE.match(line):
                skip_nws_machine_block = False
            continue
        if _NWS_MACHINE_BLOCK_START_RE.match(line):
            skip_nws_machine_block = True
            continue
        if _NWS_COORDINATE_ROW_RE.match(line):
            continue

        # Kill pure control/footer markers
        if _SKIP_LINE_RE.match(line):
            continue

        # Verbalize URLs rather than silently dropping them (mirrors real NWR behaviour).
        line = _URL_RE.sub(lambda m: " " + verbalize_url(m.group(0)) + " ", line).strip()

        # Drop "link" or similar orphan words after URL removal
        if line.lower() in {"link", "links"}:
            continue

        # Drop obvious WMO / issued / masthead header lines
        if _WMO_HEADER_RE.match(line):
            continue
        if _NWS_ISSUED_LINE_RE.match(line):
            continue
        if _PRODUCT_MASTHEAD_RE.match(line):
            continue

        low = line.lower()

        # Drop very common footer lines
        if any(low.startswith(p) for p in _FOOTER_PREFIXES):
            continue

        # HWO: skip the entire zone-name wall between the zone-code line and the start of prose
        # (LWX HWOs repeat this block for multiple area groupings.)
        if is_hwo:
            if skip_hwo_zone_block:
                if low.startswith("this hazardous weather outlook is for"):
                    skip_hwo_zone_block = False
                    # fall through and process this line
                else:
                    continue

            if _is_zoneish_line(line):
                skip_hwo_zone_block = True
                continue

            # HWO: make section lines speakable
            if line.startswith("."):
                m = _HWO_SECTION_LINE_RE.match(line)
                if m:
                    title = m.group("title").strip().title()
                    rest = (m.group("rest") or "").strip()
                    if rest:
                        if not rest.endswith((".", "!", "?")):
                            rest += "."
                        line = f"{title}. {rest}"
                    else:
                        line = f"{title}."

        # Drop short “meta-ish” lines that look like headers, IDs, or routing metadata
        # BUT: HWO prose is frequently ALL CAPS per-line. Don't delete it just for being uppercase.
        if _METAISH_RE.match(line) and (" " not in line or line == line.upper()):
            if not is_hwo:
                # Try to keep things that look like real sentences
                if not _looks_like_all_caps_prose(line) and not any(ch in line for ch in (".", ",", "!", "?", "'")):
                    continue
            else:
                # In HWO, only drop meta-ish if it's a pure token (no spaces)
                if " " not in line and not any(ch in line for ch in (".", ",", "!", "?", "'")):
                    continue

        # Keep NWS prose out of screaming caps so VoiceText does not spell
        # ordinary words (for example, ``AT``) as individual letters.
        line = _sentence_case_nws_prose(line)

        # Belt-and-suspenders removal for coordinate rows that were flattened
        # onto the end of a prose line by an upstream formatter.
        line = _NWS_COORDINATE_TAIL_RE.sub("", line).rstrip()
        if not line:
            continue

        # Normalize compact NWS local times before VT-Paul or other TTS sees them.
        line = normalize_nws_spoken_times(line)

        # Normalize whitespace *within* the line
        line = _SPACE_RE.sub(" ", line).strip()
        lines_out.append(line)

    # Keep line breaks for pacing, but collapse excessive blankness
    out = "\n".join(lines_out).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def _festival_voice_expr(voice: str) -> str:
    """
    Accept:
      - kal_diphone
      - voice_kal_diphone
      - (voice_kal_diphone)
    Return a safe Festival expression like: (voice_kal_diphone)
    """
    v = (voice or "").strip()
    if not v:
        v = "kal_diphone"

    if v.startswith("(") and v.endswith(")"):
        v = v[1:-1].strip()

    if not v.startswith("voice_"):
        v = f"voice_{v}"

    return f"({v})"


def _duration_stretch_from_wpm(rate_wpm: int, baseline_wpm: int = 175) -> float:
    """
    Festival speed knob:
      Duration_Stretch > 1.0 => slower
      Duration_Stretch < 1.0 => faster
    We map requested WPM roughly around a baseline.
    """
    wpm = max(80, min(400, int(rate_wpm)))
    stretch = baseline_wpm / float(wpm)
    return max(0.5, min(2.0, stretch))


def _clamp_int(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(val)))


@contextmanager
def _file_lock(lock_path: Path, timeout_s: float = 30.0, poll_s: float = 0.1):
    """Simple O_EXCL lock to prevent concurrent runs clobbering input/output."""
    start = time.time()
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
            break
        except FileExistsError:
            if time.time() - start > timeout_s:
                raise RuntimeError(f"Timed out waiting for lock {lock_path}")
            time.sleep(poll_s)

    try:
        yield
    finally:
        try:
            if fd is not None:
                os.close(fd)
        except Exception:
            pass
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


@contextmanager
def _flock_path(lock_path: Path, timeout_s: float = 90.0, poll_s: float = 0.1):
    """Process-safe advisory lock that is released automatically on crash/exit."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    with lock_path.open("a+") as fh:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() - start > timeout_s:
                    raise RuntimeError(f"Timed out waiting for lock {lock_path}")
                time.sleep(poll_s)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@dataclass
class TTS:
    backend: str
    voice: str
    rate_wpm: int
    volume: float
    sample_rate: int
    text_overrides: list[dict] | None = None
    vtp_cfg: object = None  # VoiceTextPaulConfig | None
    admission_check: Callable[[], None] | None = None
    activity_context: Callable[[], AbstractContextManager[None]] | None = None

    def _voicetext_available(self) -> bool:
        state_base = Path(
            os.getenv(
                "SEASONALWEATHER_DATA_BASE",
                "/var/lib/seasonalweather",
            )
        )
        engine_root = Path(
            os.getenv(
                "VOICETEXT_PAUL_ENGINE_ROOT",
                str(state_base / "voices/voicetext_paul/WeatherRadioSuite-LIB"),
            )
        )
        engine_dir = Path(
            os.getenv(
                "VOICETEXT_PAUL_BIN_DIR",
                str(engine_root / "binary"),
            )
        )
        return (
            (engine_dir / "voicetext_paul.exe").is_file()
            and Path("/usr/local/bin/voicetext_paul_synth").is_file()
            and bool(shutil.which("sudo"))
        )

    def availability(self) -> tuple[bool, str]:
        """Check required local artifacts without synthesizing or mutating state."""
        if not shutil.which("ffmpeg"):
            return False, "ffmpeg_unavailable"
        required_binary = {
            "piper": "piper",
            "festival": "text2wave",
            "espeak-ng": "espeak-ng",
            "espeak_ng": "espeak-ng",
            "espeak": "espeak-ng",
        }.get(self.backend)
        if required_binary is not None:
            return (True, "tts_available") if shutil.which(required_binary) else (False, "backend_unavailable")
        if self.backend == "dectalk":
            say_bin = Path("/opt/dectalk/dectalk/dist/say")
            available = say_bin.is_file() and bool(shutil.which("dectalk-env"))
            return (True, "tts_available") if available else (False, "backend_unavailable")
        if self.backend == "voicetext_paul":
            return (True, "tts_available") if self._voicetext_available() else (False, "backend_unavailable")
        return False, "backend_unsupported"

    def synth_to_wav(self, text: str, out_wav: Path) -> None:
        if self.admission_check is not None:
            self.admission_check()
        if self.activity_context is not None:
            with self.activity_context():
                self._synth_to_wav_impl(text, out_wav)
            return
        self._synth_to_wav_impl(text, out_wav)

    def _synth_to_wav_impl(self, text: str, out_wav: Path) -> None:
        out_wav.parent.mkdir(parents=True, exist_ok=True)

        msg = clean_for_tts(text)
        msg = _apply_text_overrides(msg, self.text_overrides)
        tmp_wav = out_wav.with_suffix(".tmp.wav")

        try:
            if self.backend == "piper":
                if not shutil.which("piper"):
                    raise RuntimeError("piper backend selected but piper binary not found")

                cmd = ["piper", "-m", self.voice, "-f", str(tmp_wav), "-r", str(self.sample_rate)]
                subprocess.run(cmd, input=msg.encode("utf-8"), check=True)

            elif self.backend == "festival":
                if not shutil.which("text2wave"):
                    raise RuntimeError("festival backend selected but text2wave not found")

                voice_expr = _festival_voice_expr(self.voice)
                stretch = _duration_stretch_from_wpm(self.rate_wpm)

                with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".txt") as tf:
                    tf.write(msg + "\n")
                    text_path = tf.name

                try:
                    cmd = [
                        "text2wave",
                        "-eval",
                        f"(Parameter.set 'Duration_Stretch {stretch})",
                        "-eval",
                        voice_expr,
                        "-o",
                        str(tmp_wav),
                        text_path,
                    ]
                    subprocess.run(cmd, check=True)
                finally:
                    try:
                        Path(text_path).unlink(missing_ok=True)
                    except Exception:
                        pass

            elif self.backend == "dectalk":
                dectalk_dist = Path("/opt/dectalk/dectalk/dist")
                say_bin = dectalk_dist / "say"
                if not say_bin.exists():
                    raise RuntimeError(f"dectalk backend selected but {say_bin} not found")

                dectalk_env = shutil.which("dectalk-env")
                if not dectalk_env:
                    raise RuntimeError("dectalk backend selected but dectalk-env not found in PATH")

                # voice is speaker 0-9 for your build
                try:
                    speaker = int(str(self.voice).strip())
                except Exception:
                    speaker = 0
                speaker = _clamp_int(speaker, 0, 9)

                # DECtalk rate range 75-600
                rate = _clamp_int(self.rate_wpm, 75, 600)

                # volume float 0..1 -> percent 0..100
                vol = float(self.volume)
                if vol <= 0:
                    vol_pct = 0
                elif vol >= 1.0:
                    vol_pct = 100
                else:
                    vol_pct = _clamp_int(round(vol * 100), 0, 100)

                # Write 16-bit mono 11k PCM to tmp_wav, then we normalize w/ ffmpeg below.
                cmd = [
                    dectalk_env,
                    str(say_bin),
                    "-l",
                    "us",
                    "-s",
                    str(speaker),
                    "-r",
                    str(rate),
                    "-v",
                    str(vol_pct),
                    "-e",
                    "1",
                    "-fo",
                    str(tmp_wav),
                    "-c",
                    "-",  # stdin in clause-mode
                ]
                subprocess.run(cmd, input=(msg + "\n").encode("utf-8"), check=True)

            elif self.backend == "voicetext_paul":
                from .voicetext_paul_vtml import apply_voicetext_paul_vtml

                _vtml_on = bool(getattr(self.vtp_cfg, "vtml_lexicon", True))
                _alias_overrides = list(getattr(self.vtp_cfg, "alias_overrides", []) or [])
                _phoneme_overrides = list(getattr(self.vtp_cfg, "phoneme_overrides_x_cmu", []) or [])
                msg = apply_voicetext_paul_vtml(
                    msg,
                    vtml_lexicon=_vtml_on,
                    alias_overrides=_alias_overrides,
                    phoneme_overrides_x_cmu=_phoneme_overrides,
                )

                # VoiceText Paul via wrapper run as voicetext (or VOICETEXT_PAUL_RUN_AS) (avoids Wine crashes + perms under seasonalweather).
                state_base = Path(os.getenv("SEASONALWEATHER_DATA_BASE", "/var/lib/seasonalweather"))
                engine_root = Path(
                    os.getenv(
                        "VOICETEXT_PAUL_ENGINE_ROOT", str(state_base / "voices/voicetext_paul/WeatherRadioSuite-LIB")
                    )
                )
                engine_dir = Path(os.getenv("VOICETEXT_PAUL_BIN_DIR", str(engine_root / "binary")))
                exe = engine_dir / "voicetext_paul.exe"
                if not exe.exists():
                    raise RuntimeError(f"voicetext_paul backend selected but {exe} not found")

                synth = Path("/usr/local/bin/voicetext_paul_synth")
                if not synth.exists():
                    raise RuntimeError(f"voicetext_paul backend selected but wrapper {synth} not found")

                if not shutil.which("sudo"):
                    raise RuntimeError("voicetext_paul backend selected but sudo not found")
                # Run Wine engine as a dedicated low-priv user (default: voicetext)
                run_as = (getattr(self.vtp_cfg, "run_as", None) or "voicetext").strip() or "voicetext"

                out_src = engine_dir / "output.wav"

                retries = int(getattr(self.vtp_cfg, "retries", 1) or 1)
                retry_sleep_ms = int(getattr(self.vtp_cfg, "retry_sleep_ms", 150) or 150)
                reset_every = int(getattr(self.vtp_cfg, "reset_every", 0) or 0)
                kill_before = bool(getattr(self.vtp_cfg, "kill_before", False))

                cmd = ["sudo", "-n", "-u", run_as, str(synth)]
                lock_path = state_base / ".voicetext_paul_tts.lock"

                with _flock_path(lock_path, timeout_s=120.0):
                    calls = getattr(self, "_vt_paul_calls", 0) + 1
                    self._vt_paul_calls = calls

                    def _wineserver_kill() -> None:
                        subprocess.run(
                            [
                                "sudo",
                                "-n",
                                "-u",
                                run_as,
                                "/usr/local/bin/voicetext_paul_wineserver_kill",
                            ],
                            check=False,
                        )

                    if kill_before or (reset_every > 0 and (calls % reset_every) == 0):
                        _wineserver_kill()

                    last_err: Exception | None = None
                    for attempt in range(retries + 1):
                        out_src.unlink(missing_ok=True)
                        try:
                            subprocess.run(cmd, input=(msg + "\n").encode("utf-8"), cwd=str(engine_dir), check=True)
                            if out_src.exists() and out_src.stat().st_size >= 2000:
                                break
                            raise RuntimeError("voicetext_paul did not produce a valid output.wav")
                        except Exception as e:
                            last_err = e
                            _wineserver_kill()
                            if attempt < retries:
                                time.sleep(max(0.0, retry_sleep_ms / 1000.0))
                    else:
                        raise RuntimeError(
                            f"voicetext_paul failed after wineserver reset/retry: {last_err}"
                        ) from last_err

                    shutil.copyfile(out_src, tmp_wav)
                    out_src.unlink(missing_ok=True)
            elif self.backend in {"espeak-ng", "espeak_ng", "espeak"}:
                if not shutil.which("espeak-ng"):
                    raise RuntimeError("espeak-ng backend selected but espeak-ng not found")

                cmd = ["espeak-ng", "-v", self.voice, "-s", str(int(self.rate_wpm)), "-w", str(tmp_wav), msg]
                subprocess.run(cmd, check=True)

            else:
                raise RuntimeError(f"unsupported TTS backend selected: {self.backend!r}")

            # Normalize to <sample_rate> stereo 16-bit for clean concatenation
            if not shutil.which("ffmpeg"):
                raise RuntimeError("ffmpeg not found")

            ff_cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(tmp_wav),
                "-ar",
                str(int(self.sample_rate)),
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
            ]

            # Optional gain
            vol = float(self.volume)
            if vol > 0 and abs(vol - 1.0) > 1e-3:
                ff_cmd += ["-filter:a", f"volume={vol}"]

            ff_cmd.append(str(out_wav))
            subprocess.run(ff_cmd, check=True)

        finally:
            try:
                tmp_wav.unlink(missing_ok=True)
            except Exception:
                pass
