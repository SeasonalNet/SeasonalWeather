"""PNS classification and audio-policy helpers.

This module keeps Public Information Statement handling out of main.py.
PNS is intentionally treated as a broad container product: only configured,
coherent subtypes become cycle audio.  Tabular/report-style products are
suppressed instead of being handed to TTS as raw computer-formatted text.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from ..alerts.builder import (
    _clean_line,
    _collapse_blank_lines,
    _unwrap_soft_wrap,
    strip_nws_product_headers,
)
from ..tts.tts import clean_for_tts

log = logging.getLogger("seasonalweather.broadcast.pns")

_NWS_HEADER_ISSUED_RE = re.compile(
    r"^(?P<hhmm>\d{3,4})\s*(?P<ampm>AM|PM)\s*(?P<tz>[A-Z]{2,4})\s+"
    r"(?P<dow>[A-Za-z]{3})\s+(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})\s*$",
    re.IGNORECASE,
)
_UGC_EXPIRY_RE = re.compile(r"-(?P<dd>\d{2})(?P<hh>\d{2})(?P<mm>\d{2})-")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z'-]*\b")

_TZ_OFFSETS: dict[str, dt.tzinfo] = {
    "UTC": dt.timezone.utc,
    "GMT": dt.timezone.utc,
    "EST": dt.timezone(dt.timedelta(hours=-5), "EST"),
    "EDT": dt.timezone(dt.timedelta(hours=-4), "EDT"),
    "CST": dt.timezone(dt.timedelta(hours=-6), "CST"),
    "CDT": dt.timezone(dt.timedelta(hours=-5), "CDT"),
    "MST": dt.timezone(dt.timedelta(hours=-7), "MST"),
    "MDT": dt.timezone(dt.timedelta(hours=-6), "MDT"),
    "PST": dt.timezone(dt.timedelta(hours=-8), "PST"),
    "PDT": dt.timezone(dt.timedelta(hours=-7), "PDT"),
}
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


@dataclass(frozen=True)
class PnsSubtypeConfig:
    name: str
    enabled: bool = True
    audio: bool = True
    event: str = "Public Information Statement"
    code: str = "SPS"
    key_prefix: str = "PNS"
    intro: str = "The National Weather Service has issued the following public information statement."
    headline_contains: tuple[str, ...] = ()
    body_contains_all: tuple[str, ...] = ()
    body_contains_any: tuple[str, ...] = ()
    reject_contains: tuple[str, ...] = ()
    max_fresh_hours: float = 18.0
    require_same_day: bool = False
    max_chars: int = 1800


@dataclass(frozen=True)
class PnsPolicyConfig:
    enabled: bool = True
    default_expire_hours: float = 4.0
    hard_stop_delimiter: str = "&&"
    suppress_unknown_audio: bool = True
    reject_audio_keywords: tuple[str, ...] = (
        "spotter reports",
        "storm reports",
        "preliminary local storm report",
        "metadata",
    )
    subtypes: tuple[PnsSubtypeConfig, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PnsDecision:
    action: str  # audio | ui_only | drop | disabled | no_match | stale
    subtype: str = "unknown"
    event: str = "Public Information Statement"
    code: str = "SPS"
    key: str = ""
    headline: str = ""
    script_text: str = ""
    expires_utc: dt.datetime | None = None
    issued_utc: dt.datetime | None = None
    signals: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_audio(self) -> bool:
        return self.action == "audio" and bool(self.script_text.strip())


def default_pns_subtypes() -> tuple[PnsSubtypeConfig, ...]:
    """Default PNS subtype policy used when config.yaml omits pns.subtypes."""
    return (
        PnsSubtypeConfig(
            name="severe_weather_safety_rules",
            event="Severe Weather Safety Rules",
            code="SPS",
            key_prefix="PNS_SAFETY",
            intro="The National Weather Service has issued the following public information statement.",
            headline_contains=("...SEVERE WEATHER SAFETY RULES...",),
            max_fresh_hours=18.0,
            require_same_day=True,
            max_chars=2400,
        ),
        PnsSubtypeConfig(
            name="nwr_transmitter_outage",
            event="NOAA Weather Radio Service Announcement",
            code="SPS",
            key_prefix="PNS_NWR_SERVICE",
            intro="This is a service announcement from the National Weather Service concerning NOAA Weather Radio transmitters in the service area.",
            body_contains_all=("NOAA Weather Radio", "transmitter"),
            body_contains_any=("off the air", "offline", "out of service", "technical difficulties", "maintenance"),
            max_fresh_hours=48.0,
            require_same_day=False,
            max_chars=1400,
        ),
        PnsSubtypeConfig(
            name="nwr_transmitter_restoration",
            event="NOAA Weather Radio Service Announcement",
            code="SPS",
            key_prefix="PNS_NWR_SERVICE",
            intro="This is a service announcement from the National Weather Service concerning NOAA Weather Radio transmitters in the service area.",
            body_contains_all=("NOAA Weather Radio", "transmitter"),
            body_contains_any=("returned to service", "back on the air", "service has been restored", "restored"),
            max_fresh_hours=24.0,
            require_same_day=False,
            max_chars=1200,
        ),
    )


def _as_tuple_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(v) for v in value if str(v).strip())
    return ()


def policy_from_config(cfg: Any) -> PnsPolicyConfig:
    """Normalize AppConfig.pns-like objects into module-local config."""
    if cfg is None:
        return PnsPolicyConfig(subtypes=default_pns_subtypes())
    raw_subtypes = getattr(cfg, "subtypes", None) or default_pns_subtypes()
    subtypes: list[PnsSubtypeConfig] = []
    for raw in raw_subtypes:
        if isinstance(raw, PnsSubtypeConfig):
            subtypes.append(raw)
            continue
        subtypes.append(
            PnsSubtypeConfig(
                name=str(getattr(raw, "name", "") or "pns_subtype"),
                enabled=bool(getattr(raw, "enabled", True)),
                audio=bool(getattr(raw, "audio", True)),
                event=str(getattr(raw, "event", "Public Information Statement") or "Public Information Statement"),
                code=str(getattr(raw, "code", "SPS") or "SPS").strip().upper()[:3] or "SPS",
                key_prefix=str(getattr(raw, "key_prefix", "PNS") or "PNS"),
                intro=str(getattr(raw, "intro", "") or "The National Weather Service has issued the following public information statement."),
                headline_contains=_as_tuple_strings(getattr(raw, "headline_contains", ())),
                body_contains_all=_as_tuple_strings(getattr(raw, "body_contains_all", ())),
                body_contains_any=_as_tuple_strings(getattr(raw, "body_contains_any", ())),
                reject_contains=_as_tuple_strings(getattr(raw, "reject_contains", ())),
                max_fresh_hours=float(getattr(raw, "max_fresh_hours", 18.0)),
                require_same_day=bool(getattr(raw, "require_same_day", False)),
                max_chars=int(getattr(raw, "max_chars", 1800)),
            )
        )
    return PnsPolicyConfig(
        enabled=bool(getattr(cfg, "enabled", True)),
        default_expire_hours=float(getattr(cfg, "default_expire_hours", 4.0)),
        hard_stop_delimiter=str(getattr(cfg, "hard_stop_delimiter", "&&") or "&&"),
        suppress_unknown_audio=bool(getattr(cfg, "suppress_unknown_audio", True)),
        reject_audio_keywords=_as_tuple_strings(getattr(cfg, "reject_audio_keywords", ()))
        or PnsPolicyConfig().reject_audio_keywords,
        subtypes=tuple(subtypes),
    )


def _parse_dt(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        out = value
    elif isinstance(value, str) and value.strip():
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            out = dt.datetime.fromisoformat(s)
        except Exception:
            return None
    else:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=dt.timezone.utc)
    return out.astimezone(dt.timezone.utc)


def parse_nws_header_issued_dt(text: str, fallback: Any = None) -> dt.datetime | None:
    for raw in (text or "").splitlines()[:80]:
        s = raw.strip()
        m = _NWS_HEADER_ISSUED_RE.match(s)
        if not m:
            continue
        hhmm = m.group("hhmm")
        hour = int(hhmm[:-2])
        minute = int(hhmm[-2:])
        ampm = m.group("ampm").upper()
        if ampm == "AM":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        month = _MONTHS.get(m.group("mon").upper())
        tzinfo = _TZ_OFFSETS.get(m.group("tz").upper())
        if not month or tzinfo is None:
            continue
        try:
            return dt.datetime(
                int(m.group("year")), month, int(m.group("day")), hour, minute, tzinfo=tzinfo
            ).astimezone(dt.timezone.utc)
        except Exception:
            continue
    return _parse_dt(fallback)


def pns_text_same_issuance(
    raw_text: str,
    candidate_text: str,
    *,
    raw_fallback: Any = None,
    candidate_fallback: Any = None,
    tolerance_seconds: float = 90.0,
) -> bool:
    """Return True when two PNS texts appear to be the same issuance.

    api.weather.gov can briefly lag NWWS-OI during active issuance.  For
    VTEC-less text products such as PNS, matching only AWIPS/WFO is not enough:
    an older product can otherwise replace the live NWWS payload and make fresh
    products look stale because their UGC expiry came from the previous PNS.
    """
    raw_issued = parse_nws_header_issued_dt(raw_text, fallback=raw_fallback)
    candidate_issued = parse_nws_header_issued_dt(candidate_text, fallback=candidate_fallback)
    if raw_issued is None and candidate_issued is None:
        return True
    if raw_issued is None or candidate_issued is None:
        return False
    return abs((candidate_issued - raw_issued).total_seconds()) <= max(0.0, tolerance_seconds)


def parse_ugc_expiry_utc(text: str, issued_utc: dt.datetime | None) -> dt.datetime | None:
    if issued_utc is None:
        return None
    header_lines: list[str] = []
    for raw in (text or "").splitlines()[2:18]:
        s = raw.strip()
        if not s:
            break
        header_lines.append(s)
        if _UGC_EXPIRY_RE.search(s):
            break
    joined = "".join(header_lines)
    matches = list(_UGC_EXPIRY_RE.finditer(joined))
    if not matches:
        return None
    m = matches[-1]
    day = int(m.group("dd"))
    hour = int(m.group("hh"))
    minute = int(m.group("mm"))
    base = issued_utc.astimezone(dt.timezone.utc)
    candidates: list[dt.datetime] = []
    for month_offset in (-1, 0, 1):
        year = base.year
        month = base.month + month_offset
        if month < 1:
            month += 12
            year -= 1
        elif month > 12:
            month -= 12
            year += 1
        try:
            candidates.append(dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc))
        except ValueError:
            continue
    if not candidates:
        return None
    future = [c for c in candidates if c >= base - dt.timedelta(minutes=10)]
    return min(future, key=lambda c: abs(c - base)) if future else min(candidates, key=lambda c: abs(c - base))


def _split_spoken_candidate(text: str, delimiter: str) -> str:
    cleaned = (text or "").replace("\r", "")
    marker = (delimiter or "&&").strip()
    if marker:
        # Treat a delimiter line as a hard metadata boundary.
        pat = re.compile(rf"(?m)^\s*{re.escape(marker)}\s*$")
        m = pat.search(cleaned)
        if m:
            cleaned = cleaned[: m.start()]
    return cleaned


def _headline_lines(text: str) -> list[str]:
    stripped = strip_nws_product_headers(text or "")
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    return lines[:8]


def _contains_any(haystack: str, needles: Sequence[str]) -> bool:
    h = haystack.lower()
    return any(str(n).strip().lower() in h for n in needles if str(n).strip())


def _contains_all(haystack: str, needles: Sequence[str]) -> bool:
    h = haystack.lower()
    return all(str(n).strip().lower() in h for n in needles if str(n).strip())


def _aligned_table_rows(lines: list[str]) -> int:
    count = 0
    for ln in lines:
        s = ln.rstrip()
        if not s:
            continue
        if re.search(r"\S\s{2,}\S", s) and len(_NUMBER_RE.findall(s)) >= 2:
            count += 1
    return count


def detect_computer_block_signals(text: str) -> tuple[str, ...]:
    upper = (text or "").upper()
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    signals: list[str] = []
    if "*****METADATA*****" in upper or re.search(r"(?m)^\s*METADATA\s*$", upper):
        signals.append("metadata")
    if "LOCATION" in upper and "TIME/DATE" in upper and "COMMENTS" in upper:
        signals.append("table_header")
    if re.search(r"\b(?:PKGUST|PKSUST|SNOW|HAIL|LSR|ASOS|AWOS|MESONET|COCORAHS|NDBC|NOS-PORTS|NOS-NWLON)\b", upper):
        signals.append("report_tokens")
    rows = _aligned_table_rows(lines)
    if rows >= 8:
        signals.append("aligned_rows")
    nums = len(_NUMBER_RE.findall(text or ""))
    words = len(_WORD_RE.findall(text or ""))
    if words and nums / max(words, 1) >= 0.45 and nums >= 25:
        signals.append("numeric_dense")
    latlonish = len(re.findall(r"[-+]?\d{2}\.\d{2,}\s*,\s*[-+]?\d{2,3}\.\d{2,}", text or ""))
    if latlonish >= 3:
        signals.append("latlon_rows")
    return tuple(dict.fromkeys(signals))


def _match_subtype(text: str, subtype: PnsSubtypeConfig) -> bool:
    if not subtype.enabled:
        return False
    full_text = text or ""
    headlines = "\n".join(_headline_lines(full_text))
    if subtype.headline_contains and not _contains_any(headlines, subtype.headline_contains):
        return False
    if subtype.body_contains_all and not _contains_all(full_text, subtype.body_contains_all):
        return False
    if subtype.body_contains_any and not _contains_any(full_text, subtype.body_contains_any):
        return False
    if subtype.reject_contains and _contains_any(full_text, subtype.reject_contains):
        return False
    return True


def _build_script(text: str, subtype: PnsSubtypeConfig, delimiter: str) -> str:
    spoken_text = _split_spoken_candidate(text, delimiter)
    body_text = strip_nws_product_headers(spoken_text or "")
    lines_raw = [ln.rstrip() for ln in body_text.splitlines()]
    lines = _unwrap_soft_wrap(lines_raw)

    body: list[str] = []
    in_body = False
    for ln in lines:
        s = (ln or "").strip()
        if not in_body:
            if s.startswith("...") or "national weather service" in s.lower() or s.lower().startswith("noaa weather radio"):
                in_body = True
            else:
                continue
        if s.startswith(("&&", "$$")):
            break
        if not s:
            body.append("")
            continue
        cleaned = _clean_line(s)
        if cleaned:
            body.append(cleaned)

    body = _collapse_blank_lines(body)
    script_raw = "\n".join(body)
    script = clean_for_tts(script_raw)
    if subtype.max_chars > 0 and len(script) > subtype.max_chars:
        script = script[: subtype.max_chars].rsplit(" ", 1)[0].rstrip(" .") + "."
    intro = (subtype.intro or "").strip()
    if intro and script.strip():
        return intro + "\n\n" + script.strip()
    return script.strip()


def _sha1_12(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "ignore"), usedforsecurity=False).hexdigest()[:12]


class PnsStateMachine:
    """Classify a PNS and decide whether it is safe/useful cycle audio."""

    def __init__(self, cfg: Any, *, tz: ZoneInfo) -> None:
        self.policy = policy_from_config(cfg)
        self.tz = tz

    def evaluate(self, raw_text: str, *, wfo: str = "", awips_id: str = "", issued: Any = None, now: Any = None) -> PnsDecision:
        if not self.policy.enabled:
            return PnsDecision(action="disabled", reason="pns_disabled")

        text = (raw_text or "").replace("\r", "")
        if not text.strip():
            return PnsDecision(action="drop", reason="empty_text")

        issued_utc = parse_nws_header_issued_dt(text, fallback=issued)
        exp_utc = parse_ugc_expiry_utc(text, issued_utc)
        if now is None:
            now_utc = dt.datetime.now(dt.timezone.utc)
        else:
            now_utc = _parse_dt(now) or dt.datetime.now(dt.timezone.utc)
        if exp_utc is None:
            base = issued_utc or now_utc
            exp_utc = base + dt.timedelta(hours=max(0.25, self.policy.default_expire_hours))
        if exp_utc <= now_utc - dt.timedelta(seconds=60):
            return PnsDecision(action="stale", reason="ugc_expired", issued_utc=issued_utc, expires_utc=exp_utc)

        spoken_candidate = _split_spoken_candidate(text, self.policy.hard_stop_delimiter)
        full_signals = list(detect_computer_block_signals(text))
        spoken_signals = [s for s in detect_computer_block_signals(spoken_candidate) if s != "metadata"]
        reject_keyword = _contains_any(text, self.policy.reject_audio_keywords)
        if reject_keyword:
            full_signals.append("reject_keyword")
        signals = tuple(dict.fromkeys(full_signals + spoken_signals))

        subtype = next((st for st in self.policy.subtypes if _match_subtype(text, st)), None)
        if subtype is None:
            action = "ui_only" if self.policy.suppress_unknown_audio else "drop"
            return PnsDecision(action=action, signals=signals, reason="no_configured_subtype_match", issued_utc=issued_utc, expires_utc=exp_utc)

        if not subtype.audio:
            return PnsDecision(
                action="ui_only",
                subtype=subtype.name,
                event=subtype.event,
                code=subtype.code,
                signals=signals,
                reason="subtype_audio_disabled",
                issued_utc=issued_utc,
                expires_utc=exp_utc,
            )

        if signals and not subtype.name.startswith("nwr_transmitter") and subtype.name != "severe_weather_safety_rules":
            return PnsDecision(
                action="ui_only",
                subtype=subtype.name,
                event=subtype.event,
                code=subtype.code,
                signals=signals,
                reason="computer_like_content",
                issued_utc=issued_utc,
                expires_utc=exp_utc,
            )
        if any(s in signals for s in ("table_header", "aligned_rows", "numeric_dense", "latlon_rows")) and subtype.name == "severe_weather_safety_rules":
            return PnsDecision(
                action="ui_only",
                subtype=subtype.name,
                event=subtype.event,
                code=subtype.code,
                signals=signals,
                reason="configured_subtype_failed_coherence_gate",
                issued_utc=issued_utc,
                expires_utc=exp_utc,
            )

        now_local = now_utc.astimezone(self.tz)
        if issued_utc is None:
            return PnsDecision(action="stale", subtype=subtype.name, event=subtype.event, code=subtype.code, reason="missing_issued_time", signals=signals, expires_utc=exp_utc)
        issued_local = issued_utc.astimezone(self.tz)
        age = now_local - issued_local
        if age.total_seconds() < -300:
            return PnsDecision(action="stale", subtype=subtype.name, event=subtype.event, code=subtype.code, reason="issued_in_future", signals=signals, issued_utc=issued_utc, expires_utc=exp_utc)
        if subtype.max_fresh_hours > 0 and age > dt.timedelta(hours=subtype.max_fresh_hours):
            return PnsDecision(action="stale", subtype=subtype.name, event=subtype.event, code=subtype.code, reason="past_freshness_window", signals=signals, issued_utc=issued_utc, expires_utc=exp_utc)
        if subtype.require_same_day and issued_local.date() != now_local.date():
            return PnsDecision(action="stale", subtype=subtype.name, event=subtype.event, code=subtype.code, reason="not_same_local_day", signals=signals, issued_utc=issued_utc, expires_utc=exp_utc)

        script = _build_script(text, subtype, self.policy.hard_stop_delimiter)
        if not script.strip():
            return PnsDecision(action="drop", subtype=subtype.name, event=subtype.event, code=subtype.code, reason="no_coherent_spoken_text", signals=signals, issued_utc=issued_utc, expires_utc=exp_utc)

        key_material = f"{wfo}|{awips_id}|{subtype.name}|{text[:1200]}"
        key = f"{subtype.key_prefix}:{(wfo or '').strip()}:{_sha1_12(key_material)}"
        headline = subtype.event
        return PnsDecision(
            action="audio",
            subtype=subtype.name,
            event=subtype.event,
            code=subtype.code,
            key=key,
            headline=headline,
            script_text=script,
            expires_utc=exp_utc,
            issued_utc=issued_utc,
            signals=signals,
            reason="accepted",
        )
