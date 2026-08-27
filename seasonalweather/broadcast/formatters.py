"""Canonical, single-owner formatter and prose subsystem.

All production text/prose formatting implementations live in this module.
The former source-named formatter modules are compatibility shims only; they
must not regain independent implementations.  Wire-format adapters may parse
input, while this subsystem owns the resulting spoken and public prose.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from math import floor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ..alerts.cap_nws import CapAlertEvent
from ..alerts.product import ParsedProduct
from ..alerts.vtec import VTEC_PARSE_RE as _VTEC_PARSE_RE
from ..same.events import label_or_code, org_broadcast_prefix
from ..tts.preprocess import clean_for_tts

# --- centralized from seasonalweather/alerts/builder.py ---
_STAR_RE = re.compile(r"^\s*\*\s+")

# --- centralized from seasonalweather/alerts/builder.py ---
_SPACE_RE = re.compile(r"\s+")

# --- centralized from seasonalweather/alerts/builder.py ---
_END_PUNCT_RE = re.compile(r"(\.\.\.|[.!?:;])$")

# --- centralized from seasonalweather/alerts/builder.py ---
_UGC_RE = re.compile(r"^[A-Z]{2}[CZ]\d{3}(?:-\d{3})*-\d{6}-?$")

# --- centralized from seasonalweather/alerts/builder.py ---
_WMO_RE = re.compile(r"^[A-Z]{4}\d{2}\s+[A-Z]{4}\s+\d{6}$")

# --- centralized from seasonalweather/alerts/builder.py ---
_NWS_ISSUED_RE = re.compile(r"^\d{3,4}\s*(?:AM|PM)\s+[A-Z]{2,4}\s+[A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{4}$")

# --- centralized from seasonalweather/alerts/builder.py ---
_PRODUCT_MASTHEAD_RE = re.compile(
    r"^(?:URGENT\s*-\s*)?(?:WINTER WEATHER MESSAGE|COASTAL HAZARD MESSAGE|SPECIAL WEATHER STATEMENT|"
    r"FLOOD WARNING|FLOOD WATCH|FLOOD ADVISORY|SEVERE WEATHER STATEMENT|SEVERE THUNDERSTORM WARNING|"
    r"TORNADO WARNING|BLIZZARD WARNING|HIGH WIND WARNING|HURRICANE LOCAL STATEMENT)\s*$",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/alerts/builder.py ---
_PPA_RE = re.compile(r"^PRECAUTIONARY/PREPAREDNESS ACTIONS\b", re.IGNORECASE)

# --- centralized from seasonalweather/alerts/builder.py ---
_TZ_ABBR_RE = re.compile(
    r"\b(EDT|EST|CDT|CST|MDT|MST|PDT|PST|AKDT|AKST|HST)\b",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/alerts/builder.py ---
_AMPM_ABBR_RE = re.compile(r"\b(AM|PM)\b", re.IGNORECASE)

# --- centralized from seasonalweather/alerts/builder.py ---
_STATE_ABBRS = {
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
    "IN",
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
    "OR",
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

# --- centralized from seasonalweather/alerts/builder.py ---
_STATE_ABBRS -= {"IN", "OR"}

# --- centralized from seasonalweather/alerts/builder.py ---
_STATE_ABBR_RE = re.compile(r"\b(" + "|".join(sorted(_STATE_ABBRS)) + r")\b", re.IGNORECASE)

# --- centralized from seasonalweather/alerts/builder.py ---
def _looks_like_all_caps_prose(s: str) -> bool:
    """Return true for NWS all-caps narrative lines that should not be spelled."""
    t = (s or "").strip()
    letters = re.sub(r"[^A-Za-z]+", "", t)
    if len(letters) < 3:
        return False
    if any(ch.islower() for ch in letters):
        return False
    # Avoid converting compact identifiers / product IDs.  Multi-word warning
    # headlines and FOR-scope lines still pass this test.
    return bool(re.search(r"\s", t)) or t.startswith("...")

# --- centralized from seasonalweather/alerts/builder.py ---
def _sentence_case_all_caps_prose(s: str) -> str:
    """Make all-caps NWS prose safe for VoiceText without killing acronyms."""
    t = (s or "").strip()
    if not _looks_like_all_caps_prose(t):
        return t

    # Headline wrappers are visual markup, not words to speak.
    t = re.sub(r"^\.\.\.\s*", "", t)
    if t.endswith("..."):
        t = t[:-3].rstrip() + "."

    t = t.lower()
    if t:
        t = t[0].upper() + t[1:]

    t = _AMPM_ABBR_RE.sub(lambda m: m.group(1).upper(), t)
    t = _TZ_ABBR_RE.sub(lambda m: m.group(1).upper(), t)
    t = _STATE_ABBR_RE.sub(lambda m: m.group(1).upper(), t)
    return t

# --- centralized from seasonalweather/alerts/builder.py ---
_META_SKIP_PREFIXES = (
    "LAT...LON",
    "TIME...MOT...LOC",
    "MAX HAIL",
    "MAX WIND",
    "&&",
    "$$",
)

# --- centralized from seasonalweather/alerts/builder.py ---
_TAGS = ("HAZARD", "SOURCE", "IMPACT")

# --- centralized from seasonalweather/alerts/builder.py ---
@dataclass
class SpokenAlert:
    title: str
    script: str

# --- centralized from seasonalweather/alerts/builder.py ---
def strip_nws_product_headers(raw: str) -> str:
    """
    Remove NWS/WMO/AWIPS + UGC/VTEC boilerplate that often appears at the top of
    api.weather.gov/products/* productText (especially NWWS paths).

    Goal: prevent zone codes (e.g., VAZ025-027-...) and VTEC lines (/O.CON.../)
    from leaking into spoken/TTS output.

    Safe-by-default: if patterns aren't found, returns input mostly unchanged.
    """
    if not raw:
        return raw or ""

    s = raw.replace("\r\n", "\n").replace("\r", "\n")

    lines = s.split("\n")
    i = 0

    def is_wmo_header(line: str) -> bool:
        # e.g. "WWUS41 KLWX 180551"
        return bool(re.match(r"^[A-Z]{4}\d{2}\s+[A-Z]{4}\s+\d{6}$", line.strip()))

    def is_awips_id(line: str) -> bool:
        # e.g. "WSWLWX" (varies; keep broad but not too broad)
        t = line.strip()
        return bool(re.match(r"^[A-Z0-9]{3,16}$", t))

    def has_ugc_codes(line: str) -> bool:
        # UGC contains patterns like VAZ025, MDZ011, DCZ001 etc (Z or C)
        return bool(re.search(r"\b[A-Z]{2}[CZ]\d{3}\b", line))

    def is_vtec_line(line: str) -> bool:
        t = line.strip()
        # VTEC lines are typically wrapped in slashes and start with /O. or /T.
        return (t.startswith("/O.") or t.startswith("/T.") or t.startswith("/E.")) and t.endswith("/")

    def is_noise_line(line: str) -> bool:
        t = line.strip()
        return (
            t in {"", "NNNN", "$$", "&&"}
            or t.isdigit()
            or bool(_NWS_ISSUED_RE.match(t))
            or bool(_PRODUCT_MASTHEAD_RE.match(t))
        )

    # Drop leading empties/noise
    while i < len(lines) and is_noise_line(lines[i]):
        i += 1

    # Drop WMO header + AWIPS if present
    if i < len(lines) and is_wmo_header(lines[i]):
        i += 1
        # sometimes there is a blank line after
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        if i < len(lines) and is_awips_id(lines[i]):
            # Only drop if next looks like header-ish too (blank/UGC/VTEC)
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt == "" or has_ugc_codes(nxt) or nxt.startswith("/O."):
                i += 1

    # Drop subsequent blanks
    while i < len(lines) and lines[i].strip() == "":
        i += 1

    # Drop UGC lines at top (may wrap across multiple lines)
    # Typical: "VAZ025-027-...-180700-"
    while i < len(lines):
        t = lines[i].strip()
        if not t:
            i += 1
            continue
        if has_ugc_codes(t):
            i += 1
            # Some UGC blocks wrap; keep skipping while UGC-looking continues
            while i < len(lines) and (has_ugc_codes(lines[i]) or lines[i].strip().endswith("-")):
                i += 1
            continue
        break

    # Drop VTEC lines immediately after UGC (often multiple).  NWWS
    # products then usually include human-readable county lines and an
    # issuance timestamp before the real ...headline... block.  Those county
    # lines are metadata for targeting, not part of the spoken headline.
    saw_vtec = False
    while i < len(lines) and is_vtec_line(lines[i]):
        saw_vtec = True
        i += 1

    if saw_vtec:
        headline_idx = None
        for j in range(i, min(len(lines), i + 12)):
            if lines[j].strip().startswith("..."):
                headline_idx = j
                break
        if headline_idx is not None:
            i = headline_idx

    # Drop a blank line after headers if present
    while i < len(lines) and lines[i].strip() == "":
        i += 1

    # Now keep the rest, but scrub stray header artifacts anywhere (belt+suspenders)
    out: list[str] = []
    for ln in lines[i:]:
        t = ln.strip()
        if t in {"NNNN"}:
            continue
        if is_vtec_line(ln):
            continue
        # Occasionally a UGC line can reappear in relayed products; drop it.
        if has_ugc_codes(ln) and re.search(r"-\d{6}\b", ln):
            continue
        # Drop SAME ZCZC framing if it shows up in productText (rare but possible)
        if t.startswith("ZCZC-") and len(t) < 120:
            continue
        out.append(ln)

    # Trim excessive leading/trailing blank lines
    cleaned = "\n".join(out).strip("\n")
    return cleaned

# --- centralized from seasonalweather/alerts/builder.py ---
def _unwrap_soft_wrap(lines: List[str]) -> List[str]:
    """
    Joins NWS soft-wrapped lines (usually indented continuations).
    Keeps true paragraph breaks.
    """
    out: List[str] = []
    for raw in lines:
        ln = (raw or "").rstrip("\n")
        if not ln.strip():
            out.append("")
            continue

        indent = len(ln) - len(ln.lstrip(" \t"))
        if indent >= 2 and out and out[-1].strip():
            prev = out[-1].rstrip()
            # Join if previous line doesn't look complete.
            if not _END_PUNCT_RE.search(prev):
                out[-1] = prev + " " + ln.strip()
                continue

        out.append(ln.rstrip())
    return out

# --- centralized from seasonalweather/alerts/builder.py ---
def _collapse_blank_lines(lines: List[str]) -> List[str]:
    out: List[str] = []
    for ln in lines:
        if ln == "":
            if out and out[-1] == "":
                continue
        out.append(ln)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return out

# --- centralized from seasonalweather/alerts/builder.py ---
def _find_body_start(lines: List[str]) -> int:
    # Prefer the NWS headline marker (often where meaningful narration begins)
    for i, ln in enumerate(lines):
        s = (ln or "").strip()
        if s.startswith("...") and len(s) >= 12:
            return i

    # Otherwise prefer the normal NWS narrative intro
    for i, ln in enumerate(lines):
        s = (ln or "").strip().lower()
        if s.startswith("the national weather service"):
            return i

    # Fallback: first “has issued”
    for i, ln in enumerate(lines):
        s = (ln or "").strip().lower()
        if "has issued" in s and "national weather service" in s:
            return i
    return 0

# --- centralized from seasonalweather/alerts/builder.py ---
def _clean_line(s: str) -> str:
    s2 = _STAR_RE.sub("", (s or "").strip())

    # Never speak the PRECAUTIONARY header (but we DO speak the content after it)
    if _PPA_RE.match(s2):
        return ""

    # Convert marker ellipses to CAP-ish punctuation.  Keep labels mixed-case
    # because VoiceText Paul may spell short all-caps tokens as letters.
    for tag in _TAGS:
        if s2.startswith(tag + "..."):
            rest = s2[len(tag + "...") :].lstrip()
            label = tag.capitalize()
            s2 = f"{label}: {rest}" if rest else f"{label}."
            break

    # If a line ends with "...", turn that into a sentence-ish period.
    if s2.endswith("..."):
        s2 = s2[:-3].rstrip() + "."

    s2 = _sentence_case_all_caps_prose(s2)

    # Squash weird spacing
    s2 = _SPACE_RE.sub(" ", s2).strip()
    return s2

# --- centralized from seasonalweather/alerts/builder.py ---
def build_spoken_alert_full(parsed: ParsedProduct, official_text: str) -> SpokenAlert:
    official_text = strip_nws_product_headers(official_text)
    lines = _unwrap_soft_wrap([ln.rstrip() for ln in official_text.splitlines()])
    start = _find_body_start(lines)

    body: List[str] = []
    for ln in lines[start:]:
        s = (ln or "").strip()
        if not s:
            body.append("")
            continue

        if s.startswith(("&&", "$$")):
            break
        if any(s.startswith(pfx) for pfx in _META_SKIP_PREFIXES):
            continue
        if re.fullmatch(r"[0-9]{3,}", s):  # "000"
            continue
        if _WMO_RE.match(s):
            continue
        if _NWS_ISSUED_RE.match(s):
            continue
        if _PRODUCT_MASTHEAD_RE.match(s):
            continue
        if re.fullmatch(r"[A-Z]{6}", s):  # "SQWCTP" etc
            continue
        if _UGC_RE.match(s):
            continue
        # VTEC line (starts with / and ends with /)
        if s.startswith("/") and s.endswith("/") and "." in s:
            continue

        cleaned = _clean_line(s)
        if cleaned:
            body.append(cleaned)

    body = _collapse_blank_lines(body)

    title = f"{parsed.product_type} from {parsed.wfo}"
    script_raw = "\n".join(body)

    # Make punctuation/spacing match what your CAP path tends to sound like.
    script = clean_for_tts(strip_nws_product_headers(script_raw))
    return SpokenAlert(title=title, script=script)

# --- centralized from seasonalweather/alerts/builder.py ---
def build_spoken_alert(parsed: ParsedProduct, official_text: str) -> SpokenAlert:
    return build_spoken_alert_full(parsed, official_text)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_MARINE_UGC_RE = re.compile(r"\b(?:ANZ|AMZ|GMZ|LMZ|PHZ|PKZ|PZZ|SLZ)\d{3}\b", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_MARINE_AREA_HINTS = (
    "tidal potomac",
    "chesapeake bay",
    "atlantic coastal waters",
    "coastal waters",
    "patapsco river",
    "patuxent river",
    "harbor",
    "sound",
    "sounds",
    "inlet",
    "strait",
    "straits",
    "gulf",
    "ocean",
    "offshore",
    "nearshore",
    "open lake",
    "lake huron",
    "lake michigan",
    "lake superior",
    "lake erie",
    "marine",
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_MARINE_PHEN = {"SC", "GL", "SR", "HF", "SE", "UP", "RB", "SI", "BW", "MF", "MH", "MS", "LO", "SU", "MA"}

# --- centralized from seasonalweather/broadcast/product_text.py ---
STATE_NAME_FULL: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "the District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

# --- centralized from seasonalweather/broadcast/product_text.py ---
_TZ_NAME_MAP = {
    "EST": "Eastern Standard Time",
    "EDT": "Eastern Daylight Time",
    "CST": "Central Standard Time",
    "CDT": "Central Daylight Time",
    "MST": "Mountain Standard Time",
    "MDT": "Mountain Daylight Time",
    "PST": "Pacific Standard Time",
    "PDT": "Pacific Daylight Time",
    "AKST": "Alaska Standard Time",
    "AKDT": "Alaska Daylight Time",
    "HST": "Hawaii Standard Time",
    "AST": "Atlantic Standard Time",
    "ADT": "Atlantic Daylight Time",
    "UTC": "Coordinated Universal Time",
    "GMT": "Greenwich Mean Time",
}

# --- centralized from seasonalweather/broadcast/product_text.py ---
_NWS_HEADER_ISSUED_RE = re.compile(
    r"^(?P<hhmm>\d{3,4})\s*(?P<ampm>AM|PM)\s*(?P<tz>[A-Z]{2,4})\s+"
    r"(?P<dow>[A-Za-z]{3})\s+(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})\s*$"
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SPS_INTRO_LEAD_RE = re.compile(
    r"(?is)^(?:This is a statement from the National Weather Service\.|The National Weather Service has issued the following message\.)\s*"
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def expand_tz_token(token: str) -> str:
    tok = (token or "").strip()
    if not tok:
        return "local"
    return _TZ_NAME_MAP.get(tok.upper(), tok)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def fmt_local_from_utc_iso(iso_str: str, *, local_tz: dt.tzinfo | None = None) -> str:
    """Parse an ISO-8601 UTC timestamp and return a human-friendly local time phrase."""
    s = (iso_str or "").strip()
    if not s:
        return ""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        utc_dt = dt.datetime.fromisoformat(s)
        if local_tz is not None:
            local_dt = utc_dt.astimezone(local_tz)
        else:
            local_dt = utc_dt
        hour12 = local_dt.hour % 12 or 12
        ampm = "AM" if local_dt.hour < 12 else "PM"
        tz_name = expand_tz_token(local_dt.strftime("%Z"))
        if local_dt.minute == 0:
            return f"{hour12} {ampm} {tz_name}"
        return f"{hour12}:{local_dt.minute:02d} {ampm} {tz_name}"
    except Exception:
        return ""

# --- centralized from seasonalweather/broadcast/product_text.py ---
def nws_header_issued_phrase(text: str) -> str | None:
    """
    Extract a spoken timestamp from a common NWS header line such as:
      "310 PM EST Sun Jan 11 2026"
    """
    if not text:
        return None

    dow_map = {
        "SUN": "Sunday",
        "MON": "Monday",
        "TUE": "Tuesday",
        "WED": "Wednesday",
        "THU": "Thursday",
        "FRI": "Friday",
        "SAT": "Saturday",
    }
    mon_map = {
        "JAN": "January",
        "FEB": "February",
        "MAR": "March",
        "APR": "April",
        "MAY": "May",
        "JUN": "June",
        "JUL": "July",
        "AUG": "August",
        "SEP": "September",
        "OCT": "October",
        "NOV": "November",
        "DEC": "December",
    }

    for ln in (text or "").splitlines()[:80]:
        raw = (ln or "").strip()
        if not raw:
            continue
        m = _NWS_HEADER_ISSUED_RE.match(raw)
        if not m:
            continue

        hhmm = m.group("hhmm")
        ampm = m.group("ampm").upper()
        tz = expand_tz_token(m.group("tz").upper())
        dow = dow_map.get(m.group("dow").strip().upper(), m.group("dow").strip())
        mon = mon_map.get(m.group("mon").strip().upper(), m.group("mon").strip())
        day = str(int(m.group("day")))
        year = m.group("year")

        if len(hhmm) == 3:
            hour = int(hhmm[0])
            minute = int(hhmm[1:])
        else:
            hour = int(hhmm[:2])
            minute = int(hhmm[2:])

        return f"{hour}:{minute:02d} {ampm} {tz} {dow} {mon} {day} {year}"

    return None

# --- centralized from seasonalweather/broadcast/product_text.py ---
def sps_preamble(sent_iso: str | None = None, *, local_tz: dt.tzinfo | None = None) -> str:
    """
    Build the NWR-style Special Weather Statement preamble used by CAP and NWWS paths.
    """
    issued = fmt_local_from_utc_iso(sent_iso or "", local_tz=local_tz)
    if issued:
        return f"And now a Special Weather Statement from your National Weather Service, issued at {issued}."
    return "And now a Special Weather Statement from your National Weather Service."

# --- centralized from seasonalweather/broadcast/product_text.py ---
def fix_sps_preamble(script: str, official_text: str) -> str:
    """
    Normalize NWWS SPS narration to the same NWR-style preamble as CAP SPS.
    """
    body = (script or "").strip()
    if not body:
        return body

    issued = nws_header_issued_phrase(official_text)
    lead = "And now a Special Weather Statement from your National Weather Service."
    if issued:
        lead = f"And now a Special Weather Statement from your National Weather Service, issued at {issued}."

    out = _SPS_INTRO_LEAD_RE.sub(lead + "\n", body, count=1)
    if out == body:
        out = lead + "\n" + body

    out = re.sub(r"(?im)^\s*Special Weather Statement\.\s*", "", out, count=1)
    return out.strip()

# --- centralized from seasonalweather/broadcast/product_text.py ---
def clean_cap_text(s: str, *, limit: int = 900) -> str:
    """Normalise whitespace, collapse ellipses, strip stray AWIPS IDs."""
    s2 = (s or "").replace("\r", " ").replace("\n", " ")
    s2 = re.sub(r"\s+", " ", s2).strip()
    s2 = s2.replace("...", ". ").replace("..", ".")
    s2 = re.sub(r"^[A-Z][A-Z0-9]{4,7}\s+", "", s2)
    if len(s2) > limit:
        s2 = s2[:limit].rstrip() + "..."
    return s2

# --- centralized from seasonalweather/broadcast/product_text.py ---
def join_oxford(items: list[str]) -> str:
    """Oxford-comma join: "a", "a and b", "a, b, and c"."""
    xs = [x.strip() for x in items if x and x.strip()]
    if not xs:
        return ""
    if len(xs) == 1:
        return xs[0]
    if len(xs) == 2:
        return f"{xs[0]} and {xs[1]}"
    return ", ".join(xs[:-1]) + f", and {xs[-1]}"

# --- centralized from seasonalweather/broadcast/product_text.py ---
def parse_cap_area_by_state(
    area_desc: str,
) -> tuple[dict[str, list[str]], list[str], list[str]]:
    """
    Parse CAP areaDesc (semicolon-separated "County, ST" items).
    Returns (groups_by_state, state_order, misc_items).
    """
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    misc: list[str] = []
    for raw in re.split(r";\s*", area_desc or ""):
        s = (raw or "").strip().strip(".")
        if not s:
            continue
        if "," in s:
            name, st = s.rsplit(",", 1)
            name = name.strip()
            st = st.strip().upper()
            if st not in groups:
                groups[st] = []
                order.append(st)
            groups[st].append(name)
        else:
            misc.append(s)
    return groups, order, misc

# --- centralized from seasonalweather/broadcast/product_text.py ---
def cap_is_special_weather_statement(event: str | None) -> bool:
    return str(event or "").strip().lower() == "special weather statement"

# --- centralized from seasonalweather/broadcast/product_text.py ---
def cap_normalize_nws_headline(parameters: dict[str, list[str]] | None) -> str:
    params = parameters or {}
    nws_hl_list = params.get("NWSheadline") or []
    nws_hl = str(nws_hl_list[0]).strip() if nws_hl_list else ""
    if nws_hl and nws_hl.isupper():
        nws_hl = nws_hl.capitalize()
    return nws_hl

# --- centralized from seasonalweather/broadcast/product_text.py ---
cap_nwsheadline = cap_normalize_nws_headline

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _iter_param_values(parameters: dict[str, list[str]] | None, key: str) -> list[str]:
    params = parameters or {}
    vals = params.get(key) or []
    return [str(v).strip() for v in vals if str(v).strip()]

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _same_codes_from_event(ev: Any) -> list[str]:
    vals = getattr(ev, "same_fips", None) or []
    out: list[str] = []
    for v in vals:
        s = re.sub(r"\D+", "", str(v))
        if s:
            out.append(s.zfill(6))
    return out

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _same_codes_from_parameters(parameters: dict[str, list[str]] | None) -> list[str]:
    out: list[str] = []
    for raw in _iter_param_values(parameters, "SAME") + _iter_param_values(parameters, "FIPS6"):
        for part in re.split(r"[\s,;]+", raw):
            s = re.sub(r"\D+", "", part)
            if s:
                out.append(s.zfill(6))
    return out

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _has_marine_ugc(parameters: dict[str, list[str]] | None) -> bool:
    for raw in _iter_param_values(parameters, "UGC"):
        if _MARINE_UGC_RE.search(raw):
            return True
    return False

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _has_marine_vtec(vtec: list[str] | None) -> bool:
    for raw in vtec or []:
        m = re.search(r"/O\.[A-Z]+\.[A-Z]{4}\.([A-Z]{2})\.[A-Z]\.", str(raw).upper())
        if m and m.group(1) in _MARINE_PHEN:
            return True
    return False

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _looks_marine_text(area_desc: str | None) -> bool:
    text = str(area_desc or "").strip().lower()
    return bool(text) and any(hint in text for hint in _MARINE_AREA_HINTS)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def cap_statement_area_noun(
    *,
    event: str | None,
    area_desc: str | None,
    parameters: dict[str, list[str]] | None,
    vtec: list[str] | None,
    ev: Any | None = None,
) -> str:
    same_codes = _same_codes_from_parameters(parameters)
    if ev is not None:
        same_codes.extend(_same_codes_from_event(ev))
    if any(code.startswith("07") for code in same_codes):
        return "areas"
    if _has_marine_ugc(parameters):
        return "areas"
    if _has_marine_vtec(vtec):
        return "areas"
    if _looks_marine_text(area_desc):
        return "areas"
    return "counties"

# --- centralized from seasonalweather/broadcast/product_text.py ---
def cap_area_label(ev: Any) -> str:
    """Back-compat alias."""
    return cap_statement_area_noun(
        event=getattr(ev, "event", None),
        area_desc=getattr(ev, "area_desc", None),
        parameters=getattr(ev, "parameters", {}) or {},
        vtec=getattr(ev, "vtec", None) or [],
        ev=ev,
    )

# --- centralized from seasonalweather/broadcast/product_text.py ---
def cap_statement_intro(
    *,
    event: str | None,
    sent_iso: str | None,
    sps_preamble: Callable[[str | None], str],
) -> str:
    if cap_is_special_weather_statement(event):
        return sps_preamble(sent_iso)
    return "This is a statement from the National Weather Service."

# --- centralized from seasonalweather/broadcast/product_text.py ---
def cap_uses_sps_preamble(ev: Any, event: str | None) -> bool:
    """Back-compat alias."""
    return cap_is_special_weather_statement(event)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def cap_full_opening_line(
    *,
    event: str | None,
    sent_iso: str | None,
    parameters: dict[str, list[str]] | None,
    sps_preamble: Callable[[str | None], str],
) -> str:
    if not str(event or "").strip():
        return ""
    if cap_is_special_weather_statement(event):
        return sps_preamble(sent_iso)
    nws_hl = cap_normalize_nws_headline(parameters)
    if nws_hl:
        return nws_hl if nws_hl.endswith((".", "!", "?")) else nws_hl + "."
    return f"{str(event).strip()}."

# --- centralized from seasonalweather/broadcast/product_text.py ---
@dataclass(frozen=True)
class NwsAlertTextInput:
    """Canonical NWS alert text input, independent of transport.

    CAP/JSON-LD, IPAWS CAP, NWWS-OI raw products, and API backfill should
    adapt into this shape before final spoken prose is built.  Transport
    parsers may still differ; final NWS prose should live here.
    """

    event: str = ""
    headline: str = ""
    description: str = ""
    instruction: str = ""
    area_desc: str = ""
    sent_iso: str | None = None
    expires_iso: str | None = None
    parameters: dict | None = None
    vtec: list[str] | None = None
    vtec_actions: set[str] | None = None

# --- centralized from seasonalweather/broadcast/product_text.py ---
def build_nws_full_alert_script(
    text: NwsAlertTextInput,
    *,
    sps_preamble: Callable[[str | None], str],
) -> str:
    """Build central full-read NWS alert prose from canonical fields."""
    event = clean_cap_text(text.event or "", limit=120)
    desc = clean_cap_text(text.description or "", limit=1200)
    instr = clean_cap_text(text.instruction or "", limit=700)
    params = text.parameters or {}

    lines: list[str] = []
    opening = cap_full_opening_line(
        event=event,
        sent_iso=text.sent_iso,
        parameters=params,
        sps_preamble=sps_preamble,
    )
    if opening:
        lines.append(opening)

    if desc:
        lines.append(desc)

    if instr:
        lines.append("Instructions.")
        lines.append(instr)

    return "\n".join(ln.strip() for ln in lines if ln and ln.strip()).strip()

# --- centralized from seasonalweather/broadcast/product_text.py ---
def build_nws_voice_alert_script(
    text: NwsAlertTextInput,
    *,
    sps_preamble: Callable[[str | None], str],
) -> str:
    """Build central voice-only NWS alert/update prose from canonical fields."""
    event = clean_cap_text(text.event or "", limit=120)
    desc = clean_cap_text(text.description or "", limit=900)
    instr = clean_cap_text(text.instruction or "", limit=500)
    params = text.parameters or {}
    nws_hl = cap_normalize_nws_headline(params)
    is_sps = cap_is_special_weather_statement(event)

    lines: list[str] = []
    if event:
        lines.append(
            cap_statement_intro(
                event=event,
                sent_iso=text.sent_iso,
                sps_preamble=sps_preamble,
            )
        )

        if not is_sps:
            if nws_hl:
                lines.append(nws_hl if nws_hl.endswith((".", "!", "?")) else nws_hl + ".")
            else:
                lines.append(f"{event}.")

    if desc:
        lines.append(desc)
    if instr:
        lines.append("Instructions.")
        lines.append(instr)

    return "\n".join(ln.strip() for ln in lines if ln and ln.strip()).strip()

# --- centralized from seasonalweather/broadcast/product_text.py ---
_EXPIRY_SUMMARY_TZ_RE = re.compile(
    r"\b(EDT|EST|CDT|CST|MDT|MST|PDT|PST|AKDT|AKST|HST)\b",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_EXPIRY_SUMMARY_AMPM_RE = re.compile(r"\b(AM|PM)\b", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _normalize_expiry_summary_line(line: str) -> str:
    """Make NWS all-caps expiry headlines safe for TTS narration."""
    s = str(line or "").strip()
    if not s:
        return ""

    # SVS expiry headlines often arrive as all caps.  VoiceText Paul can spell
    # short words such as "AT" as separate letters, so sentence-case the
    # summary before the common TTS pipeline sees it.  Restore clock/time-zone
    # abbreviations that should remain uppercase.
    if s.upper() == s:
        s = s.capitalize()
        s = _EXPIRY_SUMMARY_AMPM_RE.sub(lambda m: m.group(1).upper(), s)
        s = _EXPIRY_SUMMARY_TZ_RE.sub(lambda m: m.group(1).upper(), s)

    if s and not s.endswith((".", "!", "?")):
        s += "."
    return s

# --- centralized from seasonalweather/broadcast/product_text.py ---
def cap_expiry_summary_line(text: str) -> str:
    """
    Extract a single-sentence expiry summary from product or headline text.
    Returns "" if no NWS expiry phrase is found.
    """
    src = str(text or "").strip()
    if not src:
        return ""
    flat = re.sub(r"\s+", " ", src)
    m = re.search(
        r"([^.]{0,220}\b(?:will expire|has expired|has been allowed to expire"
        r"|has ended|is no longer in effect|the threat has ended)\b[^.]{0,220}[.?!]?)",
        flat,
        flags=re.IGNORECASE,
    )
    if not m:
        return ""
    return _normalize_expiry_summary_line(m.group(1))

# --- centralized from seasonalweather/broadcast/product_text.py ---
def cap_prefers_statement_update_script(event: str, vtec_actions: set[str]) -> bool:
    """
    True when a CAP/NWWS event should use the lighter statement-style EXP/CAN
    narration rather than the warning-style builder.

    Applies to advisory, statement, and message class events only.
    Warnings (including Special Marine Warning / MA.W) return False.
    """
    e = (event or "").strip().lower()
    if not e:
        return False
    if not (vtec_actions & {"CAN", "EXP"}):
        return False
    return e.endswith("advisory") or e.endswith("statement") or e.endswith("message")

# --- centralized from seasonalweather/broadcast/product_text.py ---
def expiry_summary_script(official_text: str) -> str | None:
    """
    Build a minimal voice script from an NWWS product that carries only an
    expiry/cancellation paragraph.  Returns None if no suitable sentence found.
    """
    flat = re.sub(r"\s+", " ", str(official_text or "")).strip()
    line = cap_expiry_summary_line(flat)
    if not line:
        return None
    return line

# --- centralized from seasonalweather/broadcast/product_text.py ---
def build_statement_vtec_action_script(
    *,
    event: str,
    area_desc: str,
    description: str,
    headline: str,
    vtec: list[str],
    vtec_actions: set[str],
    parameters: dict | None,
    sps_preamble: Callable[[str | None], str],
    sent_iso: str | None = None,
) -> str:
    """
    Lighter-weight voice cut-in for advisory / statement / message EXP/CAN.
    Sounds like a short NWR-style statement rather than a full warning read.
    """
    event = clean_cap_text(event or "", limit=120)
    groups, order, misc = parse_cap_area_by_state(area_desc)

    def _county_segs() -> str:
        if not groups:
            return clean_cap_text(area_desc or "the affected areas", limit=400)
        parts: list[str] = []
        for st in order:
            st_full = STATE_NAME_FULL.get(st, st)
            county_list = join_oxford(groups[st])
            if county_list:
                parts.append(f"in {st_full}, {county_list}")
        if misc:
            parts.append(join_oxford(misc))
        return "; ".join(parts) if parts else clean_cap_text(area_desc or "the affected areas", limit=400)

    summary_line = ""
    if vtec_actions & {"EXP"}:
        summary_line = cap_expiry_summary_line(description) or cap_expiry_summary_line(headline)
        if not summary_line and event:
            summary_line = f"The {event} has expired."
    elif vtec_actions & {"CAN"}:
        summary_line = cap_expiry_summary_line(description) or cap_expiry_summary_line(headline)
        if not summary_line and event:
            summary_line = f"The {event} has been cancelled."

    lines: list[str] = []
    lines.append(cap_statement_intro(event=event, sent_iso=sent_iso, sps_preamble=sps_preamble))
    area_line = _county_segs()
    if area_line:
        area_noun = cap_statement_area_noun(
            event=event,
            area_desc=area_desc,
            parameters=parameters,
            vtec=vtec,
        )
        lines.append(f"For the following {area_noun}: {area_line}.")
    if summary_line:
        lines.append(summary_line)
    elif event:
        lines.append(f"The {event} has been updated.")
    return "\n".join(ln.strip() for ln in lines if ln and ln.strip()).strip()

# --- centralized from seasonalweather/broadcast/product_text.py ---
def build_warning_vtec_action_script(
    *,
    event: str,
    headline: str,
    description: str,
    instruction: str,
    area_desc: str,
    vtec_actions: set[str],
    exp_phrase: str,
) -> str:
    """
    NWR-style voice script for VTEC update actions on warnings (non-watch).

    CON/EXT -> "remains in effect until"
    CAN     -> "has been cancelled"
    EXP     -> "has been allowed to expire"
    EXA/EXB -> "has been expanded"
    """
    event = clean_cap_text(event or "", limit=120)
    headline = clean_cap_text(headline or "", limit=280)
    description = clean_cap_text(description or "", limit=800)
    instruction = clean_cap_text(instruction or "", limit=400)

    lines: list[str] = []

    if vtec_actions & {"CAN"}:
        lines.append(f"The {event} for the following areas has been cancelled.")
        if area_desc:
            lines.append(f"Areas: {area_desc}.")
        if headline:
            lines.append(headline if headline.endswith((".", "!", "?")) else headline + ".")

    elif vtec_actions & {"EXP"}:
        lines.append(f"The {event} for the following areas has been allowed to expire.")
        if area_desc:
            lines.append(f"Areas: {area_desc}.")

    elif vtec_actions & {"EXA", "EXB"}:
        lines.append(f"The {event} has been expanded.")
        if area_desc:
            lines.append(f"This now includes: {area_desc}.")
        if exp_phrase:
            lines.append(f"This warning remains in effect until {exp_phrase}.")
        if description:
            lines.append(description)
        if instruction:
            lines.append(instruction)

    elif vtec_actions & {"EXT"}:
        lines.append(f"The {event} has been extended.")
        if area_desc:
            lines.append(f"For the following areas: {area_desc}.")
        if exp_phrase:
            lines.append(f"This warning is now in effect until {exp_phrase}.")
        if description:
            lines.append(description)
        if instruction:
            lines.append(instruction)

    else:  # CON and anything else
        if headline:
            lines.append(headline if headline.endswith((".", "!", "?")) else headline + ".")
        elif event:
            lead = f"A {event} remains in effect"
            if exp_phrase:
                lead += f" until {exp_phrase}"
            lead += "."
            lines.append(lead)
        if area_desc:
            lines.append(f"For the following areas: {area_desc}.")
        if description:
            lines.append(description)
        if instruction:
            lines.append(instruction)

    return "\n".join(ln.strip() for ln in lines if ln and ln.strip()).strip()

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_VTEC_RE = re.compile(
    r"/[A-Z]\.(?P<act>[A-Z]{3})\.[A-Z]{4}\.[A-Z0-9]{2}\.[A-Z]\.\d{4}\.",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_HEADLINE_RE = re.compile(r"^\.\.\.(.+?)\.\.\.$")

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_UNTIL_RE = re.compile(r"\bUNTIL\s+([\d:]+\s*(?:AM|PM)\s+[A-Z]{2,4})", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_AREA_INTRO_RE = re.compile(
    r"^(?:The\s+affected\s+areas?\s*(?:were|are|include[sd]?)?|"
    r"For\s+the\s+following\s+areas?)[.\s]*",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_META_RE = re.compile(
    r"^(?:LAT\.\.\.LON|TIME\.\.\.MOT\.\.\.LOC|HAIL\.\.\.|WIND\.\.\.|NNNN)",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_MACHINE_BLOCK_START_RE = re.compile(
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

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_PPA_RE = re.compile(r"^PRECAUTIONARY/PREPAREDNESS ACTIONS", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_LOC_RE = re.compile(r"^Locations?\s+(?:impacted|affected)\s+include", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_ACTION_LABEL_RE = re.compile(r"^(?:CANCELLED|CONTINUED|EXPIRED)(?:\.{1,3})?$", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_SCOPE_HEADER_RE = re.compile(
    r"^FOR\s+[A-Z0-9 ,/\-]+(?:COUNTY|COUNTIES|PARISH|PARISHES|CITY|CITIES|BOROUGH|BOROUGHS)\.?\.?.*$"
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_UGC_RE = re.compile(
    r"^(?:[A-Z]{2}[CZ]\d{3}|\d{3})(?:-(?:[A-Z]{2}[CZ]\d{3}|\d{3}))*-\d{6}-?$",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_SEG_TIMESTAMP_RE = re.compile(r"^\d{3,4}\s+(?:AM|PM)\s+[A-Z]{2,4}")

# --- centralized from seasonalweather/broadcast/product_text.py ---
_TZ_FIX_RE = re.compile(r"\b(EDT|EST|CDT|CST|MDT|MST|PDT|PST|AKDT|AKST|HST)\b", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_AMPM_FIX_RE = re.compile(r"\b(AM|PM)\b", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _fix_headline_case(h: str) -> str:
    """ALL-CAPS NWS headline → sentence case, preserving TZ abbreviations."""
    if not h.isupper():
        return h
    # In NWS geographic names, ``ST.`` is an abbreviation for ``Saint``.
    # Expanding it before lower-casing prevents VoiceText Paul from reading it
    # as ``Street`` (for example, ``ST. MARYS``).
    h = re.sub(r"\bST\.\s+(?=[A-Z])", "Saint ", h)
    h = h.capitalize()
    h = re.sub(
        r"\bsaint\s+([a-z])",
        lambda m: f"Saint {m.group(1).upper()}",
        h,
    )
    h = _TZ_FIX_RE.sub(lambda m: m.group(1).upper(), h)
    h = _AMPM_FIX_RE.sub(lambda m: m.group(1).upper(), h)
    return h

# --- centralized from seasonalweather/broadcast/product_text.py ---
@dataclass
class NwwsProductSegment:
    """One $$-delimited section of a multi-segment NWWS product."""

    actions: set[str]  # VTEC action codes present (e.g. {"CAN"}, {"CON"})
    headline: str  # Cleaned headline from ...X... line
    area_text: str  # Pipe-joined geographic area names
    reason_text: str  # Why the event is occurring/ending (narrative prose)
    precautions: str  # PRECAUTIONARY/PREPAREDNESS content
    expiry_phrase: str

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _split_nwws_vtec_sections(product_text: str) -> list[str]:
    """Return $$-delimited NWWS sections that contain VTEC lines."""
    text = (product_text or "").replace("\r\n", "\n").replace("\r", "\n")
    sections: list[str] = []

    # $$ separates UGC/VTEC product segments.  Within a segment, && normally
    # separates the narrative body from LAT/LON / tag metadata.
    for chunk in re.split(r"(?m)^\s*\$\$\s*$", text):
        if not _SEG_VTEC_RE.search(chunk):
            continue
        body = re.split(r"(?m)^\s*&&\s*$", chunk, maxsplit=1)[0].strip("\n")
        # Some offices occasionally omit the expected ``&&`` separator before
        # LAT/LON, motion, or impact-based warning tag blocks.  Those blocks are
        # terminal machine-readable metadata; discard the marker and all of its
        # continuation rows rather than allowing coordinate rows into narration.
        body_lines: list[str] = []
        for line in body.splitlines():
            if _SEG_MACHINE_BLOCK_START_RE.match(line.strip()):
                break
            body_lines.append(line)
        body = "\n".join(body_lines).strip("\n")
        if _SEG_VTEC_RE.search(body):
            sections.append(body)

    # Fallback for malformed/single-section products that lack a $$ close.
    if not sections and _SEG_VTEC_RE.search(text):
        sections.append(re.split(r"(?m)^\s*&&\s*$", text, maxsplit=1)[0].strip("\n"))

    return sections

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _extract_wrapped_headline(lines: list[str]) -> tuple[str, int, int]:
    """Extract a possibly wrapped ...headline... block."""
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s.startswith("..."):
            continue

        parts = [s]
        end_idx = i
        if s.endswith("...") and len(s) > 6:
            raw = s
        else:
            raw = s
            for j in range(i + 1, len(lines)):
                sj = lines[j].strip()
                if not sj:
                    break
                parts.append(sj)
                end_idx = j
                raw = " ".join(parts)
                if sj.endswith("..."):
                    break

        headline = raw.strip().strip(".").strip()
        return _fix_headline_case(headline).rstrip("."), i, end_idx

    return "", -1, -1

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _clean_county_area_text(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip()).strip("-")
    text = re.sub(r"-\s*", "; ", text).strip(" ;")
    return text

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _extract_county_area_text(lines: list[str]) -> str:
    """Extract the county/city name line between VTEC and issuance time."""
    area_parts: list[str] = []
    after_vtec = False
    for ln in lines:
        s = ln.strip()
        if not after_vtec:
            if s.startswith("/") and s.endswith("/") and "." in s:
                after_vtec = True
            continue
        if not s:
            if area_parts:
                break
            continue
        if _SEG_TIMESTAMP_RE.match(s) or s.startswith("..."):
            break
        if _SEG_UGC_RE.match(s) or (s.startswith("/") and s.endswith("/") and "." in s):
            continue
        area_parts.append(s)
    return _clean_county_area_text(" ".join(area_parts))

# --- centralized from seasonalweather/broadcast/product_text.py ---
def parse_nwws_product_segments(product_text: str) -> list[NwwsProductSegment]:
    """
    Split a multi-section NWWS product into VTEC-bearing $$ sections.

    NWS warning/statements commonly use $$ between UGC/VTEC sections and &&
    inside a section before LAT/LON / machine-readable metadata.  Sections
    without any VTEC lines are silently skipped.

    Used to generate per-action narration for products that carry mixed VTEC
    actions (e.g. a partial CAN+CON where some zones are cancelled while
    others continue).
    """
    segments: list[NwwsProductSegment] = []

    for raw_sec in _split_nwws_vtec_sections(product_text):
        actions: set[str] = set()
        for m in _SEG_VTEC_RE.finditer(raw_sec):
            actions.add(m.group("act").upper())
        if not actions:
            continue

        lines = [ln.rstrip() for ln in raw_sec.splitlines()]

        headline, headline_idx, headline_end_idx = _extract_wrapped_headline(lines)

        # Expiry phrase from headline
        expiry_phrase = ""
        if headline:
            um = _SEG_UNTIL_RE.search(headline.upper())
            if um:
                expiry_phrase = um.group(1).strip()

        # Area text: first prefer explicit phrases, then county/city lines after VTEC.
        area_parts: list[str] = []
        in_area = False
        area_done = False
        for ln in lines:
            s = ln.strip()
            if area_done:
                break
            if _SEG_AREA_INTRO_RE.match(s):
                in_area = True
                rest = _SEG_AREA_INTRO_RE.sub("", s).strip().rstrip(".")
                if rest:
                    area_parts.append(rest)
                continue
            if in_area:
                if not s:
                    area_done = True
                    continue
                if s.endswith("...") or (re.match(r"^[A-Z]", s) and not re.match(r"^At \d", s, re.IGNORECASE)):
                    area_parts.append(s.rstrip(".").strip())
                else:
                    area_done = True

        area_text = "; ".join(p for p in area_parts if p) or _extract_county_area_text(lines)

        # Body / reason lines and precautionary text
        body_parts: list[str] = []
        precaution_parts: list[str] = []
        in_precautions = False
        in_area_skip = False
        in_locations_block = False

        start = headline_end_idx + 1 if headline_end_idx >= 0 else 0
        for ln in lines[start:]:
            s = ln.strip()
            if s.startswith("&&") or s.startswith("$$"):
                break
            if not s:
                in_area_skip = False
                in_locations_block = False
                continue
            if _SEG_UGC_RE.match(s) or _SEG_TIMESTAMP_RE.match(s):
                continue
            if _SEG_ACTION_LABEL_RE.match(s) or _SEG_SCOPE_HEADER_RE.match(s):
                continue
            if s.startswith("/") and s.endswith("/") and "." in s:
                continue
            if _SEG_HEADLINE_RE.match(s) or _SEG_META_RE.match(s):
                continue
            if _SEG_PPA_RE.match(s):
                in_precautions = True
                in_locations_block = False
                continue
            if _SEG_AREA_INTRO_RE.match(s):
                in_area_skip = True
                continue
            if _SEG_LOC_RE.match(s):
                body_parts.append("Locations impacted include:")
                in_locations_block = True
                continue
            if in_locations_block:
                body_parts.append(s)
                continue
            if in_area_skip:
                if s.endswith("...") or re.match(r"^[A-Z][a-z]+,", s):
                    continue
                in_area_skip = False
            tag_m = re.match(r"^([A-Z]+)\.\.\.(.*?)\.?$", s)
            if tag_m and tag_m.group(1) in {"HAZARD", "SOURCE", "IMPACT"}:
                tag_text = tag_m.group(2).strip().rstrip(".")
                if tag_text:
                    body_parts.append(f"{tag_m.group(1).capitalize()}: {tag_text}.")
                else:
                    body_parts.append(f"{tag_m.group(1).capitalize()}.")
                continue
            if (
                not in_precautions
                and body_parts
                and body_parts[-1].startswith(("Hazard:", "Source:", "Impact:"))
                and ln[:1].isspace()
            ):
                body_parts[-1] = f"{body_parts[-1].rstrip('.')} {s.rstrip('.')}."
                continue
            if in_precautions:
                precaution_parts.append(s)
            else:
                body_parts.append(s)

        segments.append(
            NwwsProductSegment(
                actions=actions,
                headline=headline,
                area_text=area_text,
                reason_text=" ".join(body_parts).strip(),
                precautions=" ".join(precaution_parts).strip(),
                expiry_phrase=expiry_phrase,
            )
        )

    return [s for s in segments if s.actions]

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _ensure_sentence(text: str) -> str:
    s = (text or "").strip()
    if s and not s.endswith((".", "!", "?")):
        s += "."
    return s

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _reason_starts_with_event_terminal_scope(event_label: str, reason_text: str) -> bool:
    """Return true when body prose already contains a scoped terminal line."""
    event = re.sub(r"\s+", " ", (event_label or "").strip())
    reason = re.sub(r"\s+", " ", (reason_text or "").strip())
    if not event or not reason:
        return False

    event_re = re.escape(event)
    terminal_re = (
        rf"^the\s+{event_re}\s+(?:"
        r"is\s+cancelled|has\s+been\s+cancelled|"
        r"will\s+expire|has\s+expired|"
        r"has\s+been\s+allowed\s+to\s+expire|"
        r"will\s+be\s+allowed\s+to\s+expire"
        r")\b"
    )
    return re.search(terminal_re, reason, flags=re.IGNORECASE) is not None

# --- centralized from seasonalweather/broadcast/product_text.py ---
def build_nwws_partial_cancel_script(
    event_label: str,
    segments: list[NwwsProductSegment],
) -> str:
    """
    Build a voice script for a partial CAN+CON NWWS product.

    For example, an MWS where one zone is cancelled while other zones
    continue:

        "The Special Marine Warning has been cancelled for the Tidal Potomac
         from Cobb Island MD to Smith Point VA. The thunderstorms have moved
         out of the warned area and no longer pose a significant threat.
         The Special Marine Warning remains in effect until 3:15 PM Eastern
         Daylight Time for the Chesapeake Bay from Drum Point MD to Smith
         Point VA, and Tangier Sound. Move to safe harbor."
    """
    event = (event_label or "Weather Alert").strip()
    lines: list[str] = []

    can_segs = [s for s in segments if "CAN" in s.actions]
    exp_segs = [s for s in segments if "EXP" in s.actions]
    con_segs = [s for s in segments if "CON" in s.actions or "EXT" in s.actions]

    def _headline_or_fallback(seg: NwwsProductSegment, fallback: str) -> str:
        headline = (seg.headline or "").strip()
        if headline:
            return headline if headline.endswith((".", "!", "?")) else headline + "."
        return fallback

    for seg in can_segs:
        area = seg.area_text or "some areas"
        if _reason_starts_with_event_terminal_scope(event, seg.reason_text):
            lines.append(_ensure_sentence(seg.reason_text))
        else:
            lines.append(
                _headline_or_fallback(
                    seg,
                    f"The {event} has been cancelled for the following areas: {area}.",
                )
            )
            if seg.reason_text:
                lines.append(_ensure_sentence(seg.reason_text))

    for seg in exp_segs:
        area = seg.area_text or "some areas"
        if _reason_starts_with_event_terminal_scope(event, seg.reason_text):
            lines.append(_ensure_sentence(seg.reason_text))
        else:
            lines.append(
                _headline_or_fallback(
                    seg,
                    f"The {event} has been allowed to expire for the following areas: {area}.",
                )
            )
            if seg.reason_text:
                lines.append(_ensure_sentence(seg.reason_text))

    for seg in con_segs:
        area = seg.area_text or "other areas"
        exp_part = f" until {seg.expiry_phrase}" if seg.expiry_phrase else ""
        lines.append(
            _headline_or_fallback(
                seg,
                f"A {event} remains in effect{exp_part} for the following areas: {area}.",
            )
        )
        if seg.reason_text:
            lines.append(f"{seg.reason_text.rstrip('.')}.")
        if seg.precautions:
            lines.append(f"{seg.precautions.rstrip('.')}.")

    if not lines:
        return ""
    return "\n".join(ln.strip() for ln in lines if ln and ln.strip()).strip()

# --- centralized from seasonalweather/broadcast/product_text.py ---
def build_nwws_terminal_cancel_expiry_script(
    event_label: str,
    product_text: str,
) -> str:
    """
    Build detailed narration for a pure NWWS CAN/EXP product.

    Unlike expiry_summary_script(), this keeps product body prose that names the
    cancelled/expired area and remaining public instructions.  It intentionally
    refuses mixed continuation products; those stay on the partial-cancel path.
    """
    segments = parse_nwws_product_segments(product_text)
    if not segments:
        return ""

    terminal_actions = {"CAN", "EXP"}
    continuing_actions = {"CON", "EXT", "EXA", "EXB", "NEW"}
    terminal_segments: list[NwwsProductSegment] = []

    for seg in segments:
        if seg.actions & continuing_actions:
            return ""
        if seg.actions & terminal_actions:
            terminal_segments.append(seg)

    if len(terminal_segments) != len(segments):
        return ""

    return build_nwws_partial_cancel_script(event_label, terminal_segments)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_WATCH_VTEC_RE = re.compile(
    r"/O\.(?P<action>[A-Z]{3})\.(?P<office>[A-Z]{4})\."
    r"(?P<phen>[A-Z]{2})\.(?P<sig>[A-Z])\."
    r"(?P<etn>\d{4})\."
    r"(?P<start>\d{6,8}T\d{4}Z)-(?P<end>\d{6,8}T\d{4}Z)/"
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_STATE_ABBR_BY_FULL = {v.upper(): k for k, v in STATE_NAME_FULL.items()}

# --- centralized from seasonalweather/broadcast/product_text.py ---
_STATE_ABBR_BY_FULL["DISTRICT OF COLUMBIA"] = "DC"

# --- centralized from seasonalweather/broadcast/product_text.py ---
_STATE_ABBR_BY_FULL["THE DISTRICT OF COLUMBIA"] = "DC"

# --- centralized from seasonalweather/broadcast/product_text.py ---
_WCN_AREA_STOP_RE = re.compile(
    r"^(?:THIS INCLUDES THE CITIES|PRECAUTIONARY/PREPAREDNESS|&&|\$\$|LAT\.\.\.LON|TIME\.\.\.MOT\.\.\.LOC|NNNN\b)",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
_WCN_STATE_COUNT_RE = re.compile(
    r"^IN (?P<state>[A-Z ]+?) "
    r"(?:(?:THIS )?(?:WATCH INCLUDES|CANCELS|ALLOWS TO EXPIRE|ALLOWED TO EXPIRE)|THE NEW WATCH INCLUDES) \d+ "
    r"(?P<kind>COUNTY|COUNTIES|CITY|CITIES|INDEPENDENT CITIES)\b",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _watch_vtec_match(raw: object, wanted_action: str) -> re.Match[str] | None:
    match = _WATCH_VTEC_RE.search(str(raw).strip().upper())
    if match is None or (wanted_action and match.group("action") != wanted_action):
        return None
    if match.group("sig") != "A" or match.group("phen") not in {"TO", "SV"}:
        return None
    return match

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _parse_watch_vtec(vtec: list[str] | None, *, action: str | None = None) -> dict[str, Any] | None:
    wanted_action = (action or "").strip().upper()
    for raw in vtec or []:
        m = _watch_vtec_match(raw, wanted_action)
        if m is None:
            continue
        try:
            watch_number = int(m.group("etn"))
        except Exception:
            watch_number = None
        return {
            "kind": "tornado" if m.group("phen") == "TO" else "severe",
            "action": m.group("action"),
            "watch_number": watch_number,
            "end_utc": _parse_vtec_time_utc(m.group("end")),
        }
    return None

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _parse_vtec_time_utc(token: str) -> dt.datetime | None:
    s = (token or "").strip().upper()
    m = re.fullmatch(r"(\d{6}|\d{8})T(\d{4})Z", s)
    if not m:
        return None
    d, hm = m.group(1), m.group(2)
    try:
        if len(d) == 8:
            year, month, day = int(d[:4]), int(d[4:6]), int(d[6:8])
        else:
            year, month, day = 2000 + int(d[:2]), int(d[2:4]), int(d[4:6])
        return dt.datetime(year, month, day, int(hm[:2]), int(hm[2:]), tzinfo=dt.timezone.utc)
    except Exception:
        return None

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _watch_time_phrase(
    end_utc: dt.datetime | None, *, local_tz: dt.tzinfo | None, now: dt.datetime | None = None
) -> str:
    if end_utc is None:
        return ""
    tz = local_tz or dt.timezone.utc
    end_local = end_utc.astimezone(tz)
    ref = now.astimezone(tz) if now is not None else dt.datetime.now(tz=tz)

    day_delta = (end_local.date() - ref.date()).days
    if end_local.hour == 0 and end_local.minute == 0:
        if day_delta == 1:
            return "midnight tonight"
        if day_delta == 0:
            return "midnight"
        return f"midnight on {end_local.strftime('%A')}"

    hour12 = end_local.hour % 12 or 12
    ampm = "AM" if end_local.hour < 12 else "PM"
    t = f"{hour12} {ampm}" if end_local.minute == 0 else f"{hour12}:{end_local.minute:02d} {ampm}"

    if end_local.hour < 12:
        part = "morning"
    elif end_local.hour < 17:
        part = "afternoon"
    elif end_local.hour < 21:
        part = "evening"
    else:
        part = "tonight"

    if day_delta == 0:
        return f"{t} tonight" if part == "tonight" else f"{t} this {part}"
    if day_delta == 1:
        return f"{t} tomorrow night" if part == "tonight" else f"{t} tomorrow {part}"
    return f"{t} on {end_local.strftime('%A')}"

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _clean_wcn_area_name(s: str) -> str:
    out = re.sub(r"\s+", " ", (s or "").strip(" .;,-\t"))
    if not out:
        return ""
    # WCN county lists are normally all-caps; title-case for speech while keeping
    # common locality words readable.
    out = out.title()
    fixes = {
        " Of ": " of ",
        " And ": " and ",
        " The ": " the ",
        "Dc": "DC",
        "'S": "'s",
    }
    for a, b in fixes.items():
        out = out.replace(a, b)
    return out

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _looks_like_wcn_area_name(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if not re.fullmatch(r"[A-Z][A-Z .'-]*(?:\s+[A-Z][A-Z .'-]*)*", s):
        return False
    bad_prefixes = (
        "WATCH COUNTY NOTIFICATION",
        "NATIONAL WEATHER SERVICE",
        "SEVERE THUNDERSTORM WATCH",
        "TORNADO WATCH",
        "THE NATIONAL WEATHER SERVICE",
        "EFFECTIVE",
        "FOR THE FOLLOWING",
        "AREAS",
        "IN EFFECT",
    )
    return not any(s.startswith(p) for p in bad_prefixes)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _split_wcn_area_line(line: str) -> list[str]:
    s = (line or "").strip()
    if not s:
        return []
    # NOAA text columns usually separate names by repeated spaces. If spacing was
    # flattened by a paste/repost, keep the line as one item rather than guessing
    # county boundaries incorrectly.
    parts = [p for p in re.split(r"\s{2,}", s) if p.strip()]
    if not parts:
        parts = [s]
    return [_clean_wcn_area_name(p) for p in parts if _clean_wcn_area_name(p)]

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _extract_wcn_area_desc(text: str) -> str:
    """
    Best-effort conversion of a WCN county block into CAP-like areaDesc.

    WCN products list areas under state/region headings instead of CAP's clean
    "County, ST; County, ST" format. This helper extracts enough structure for
    the watch script to avoid reading the entire raw WCN blob when CAP has not
    arrived yet.
    """
    # Preserve repeated spacing in county rows; WCN uses columns where two or
    # more spaces separate county names.  State/stop-line checks normalise a
    # separate uppercase copy below.
    lines = [(ln or "").strip() for ln in (text or "").splitlines()]
    if not lines:
        return ""

    start = None
    for i, line in enumerate(lines):
        if line.upper() == "AREAS" and "FOR THE FOLLOWING" in " ".join(x.upper() for x in lines[max(0, i - 3) : i + 1]):
            start = i + 1
            break
    if start is None:
        for i, line in enumerate(lines):
            if "FOR THE FOLLOWING AREAS" in line.upper():
                start = i + 1
                break
    if start is None:
        return ""

    groups: dict[str, list[str]] = {}
    order: list[str] = []
    misc: list[str] = []
    current_state: str | None = None

    def add_group(st: str, name: str) -> None:
        name2 = _clean_wcn_area_name(name)
        if not name2:
            return
        if st not in groups:
            groups[st] = []
            order.append(st)
        if name2 not in groups[st]:
            groups[st].append(name2)

    for line in lines[start:]:
        s = (line or "").strip()
        if not s:
            continue
        if _WCN_AREA_STOP_RE.match(s):
            break
        su = re.sub(r"\s+", " ", s).upper()
        if su in {"THE DISTRICT OF COLUMBIA", "DISTRICT OF COLUMBIA"}:
            misc.append("the District of Columbia")
            current_state = None
            continue
        m_state = _WCN_STATE_COUNT_RE.match(su)
        if m_state:
            st_name = re.sub(r"\s+", " ", m_state.group("state").strip())
            current_state = _STATE_ABBR_BY_FULL.get(st_name)
            continue
        if su.startswith("IN "):
            # Regional heading, e.g. "IN CENTRAL MARYLAND".
            continue
        if not current_state:
            continue
        if not _looks_like_wcn_area_name(su):
            continue
        for part in _split_wcn_area_line(s):
            add_group(current_state, part)

    parts: list[str] = []
    parts.extend(misc)
    for st in order:
        parts.extend(f"{name}, {st}" for name in groups.get(st, []))
    return "; ".join(p for p in parts if p).strip()

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _watch_area_group_phrases(groups: dict[str, list[str]], order: list[str]) -> list[str]:
    segs: list[str] = []
    for st in order:
        st_full = STATE_NAME_FULL.get(st, st)
        county_list = join_oxford(groups.get(st, []))
        if county_list:
            segs.append(f"in {st_full}: {county_list}")
    return segs

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _watch_area_sentence(area_desc: str) -> str:
    """Return NEW-watch area boilerplate.

    Lifecycle WCN sections use _watch_lifecycle_area_phrase() so CAN/EXP/CON
    narration can attach the area list directly to the action sentence.
    """
    groups, order, misc = parse_cap_area_by_state(area_desc or "")
    lines: list[str] = []

    if misc:
        lines.append("This watch includes " + join_oxford(misc) + ".")

    if groups:
        segs = _watch_area_group_phrases(groups, order)
        if len(segs) == 1 and len(order) == 1:
            lines.append("This watch includes the following counties: " + segs[0] + ".")
        elif segs:
            lines.append("This watch includes the following counties: " + "; ".join(segs) + ".")
    elif area_desc:
        lines.append(f"This watch includes the following areas: {area_desc.strip(' .')}.")

    return "\n".join(lines).strip()

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _watch_lifecycle_area_phrase(area_desc: str) -> str:
    """Return the area object for WCN lifecycle action sentences.

    Examples:
      - the following counties: in Maryland: Frederick
      - the District of Columbia and the following counties: in Maryland: Anne Arundel

    This intentionally avoids NEW-style "This watch includes..." boilerplate.
    """
    groups, order, misc = parse_cap_area_by_state(area_desc or "")
    county_phrase = ""
    if groups:
        segs = _watch_area_group_phrases(groups, order)
        if segs:
            county_phrase = "the following counties: " + "; ".join(segs)

    misc_phrase = join_oxford(misc) if misc else ""
    if misc_phrase and county_phrase:
        return f"{misc_phrase} and {county_phrase}"
    if county_phrase:
        return county_phrase
    if misc_phrase:
        return misc_phrase
    if area_desc:
        return "the following areas: " + area_desc.strip(" .")
    return ""

# --- centralized from seasonalweather/broadcast/product_text.py ---
def extract_nwws_wcn_area_desc(text: str) -> str:
    """Public wrapper for extracting CAP-like areaDesc from an NWWS WCN product."""
    return _extract_wcn_area_desc(text)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _wcn_area_match_key(name: str, state: str = "") -> str:
    """Normalise county/city + state labels for WCN areaDesc ↔ SAME-label matching."""
    n = str(name or "").strip().replace("’", "'")
    st = str(state or "").strip().upper()
    # NWS zone names sometimes include suffixes while WCN text usually omits them.
    n = re.sub(r"\b(?:COUNTY|CITY|PARISH|BOROUGH|MUNICIPALITY)\b", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\bTHE\b", " ", n, flags=re.IGNORECASE)
    blob = f"{n} {st}"
    return re.sub(r"[^a-z0-9]+", "", blob.lower())

# --- centralized from seasonalweather/broadcast/product_text.py ---
def match_nwws_wcn_area_same(area_desc: str, same_label_by_code: dict[str, str]) -> list[str]:
    """
    Match extracted WCN county/state area text against known SAME code labels.

    This intentionally uses caller-provided SAME labels, usually the configured
    service-area SAME list resolved through api.weather.gov county zones.  That
    keeps the matcher local to the deployment's allowed area instead of carrying
    a hard-coded national county FIPS table.
    """
    groups, _order, misc = parse_cap_area_by_state(area_desc or "")
    wanted: set[str] = set()
    for st, names in groups.items():
        for name in names:
            key = _wcn_area_match_key(name, st)
            if key:
                wanted.add(key)
    for raw in misc:
        m = str(raw or "").strip()
        if not m:
            continue
        key = _wcn_area_match_key(m, "")
        if key:
            wanted.add(key)
        if "district of columbia" in m.lower():
            wanted.add(_wcn_area_match_key("District of Columbia", "DC"))

    if not wanted:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for code, label in (same_label_by_code or {}).items():
        c = re.sub(r"\D+", "", str(code or ""))
        if len(c) != 6 or c in seen:
            continue
        lab = str(label or "").strip()
        if not lab:
            continue
        if "," in lab:
            name, st = lab.rsplit(",", 1)
            key = _wcn_area_match_key(name, st)
        else:
            key = _wcn_area_match_key(lab, "")
        if key in wanted:
            seen.add(c)
            out.append(c)
    return out

# --- centralized from seasonalweather/broadcast/product_text.py ---
def build_watch_reminder(kind: str) -> str:
    """Return the canonical WCN/CAP watch reminder prose."""
    if kind == "tornado":
        return (
            "Remember, a tornado watch means that conditions are favorable for the development of severe weather, "
            "including tornadoes, large hail, and damaging winds, in, and close to the watch area. "
            "While severe weather may not be imminent, persons should remain alert for rapidly changing weather conditions, "
            "and listen for later statements and possible warnings."
        )
    return (
        "Remember, a severe thunderstorm watch means that conditions are favorable for the development of severe weather, "
        "including large hail and damaging winds, in, and close to the watch area. "
        "While severe weather may not be imminent, persons should remain alert for rapidly changing weather conditions, "
        "and listen for later statements and possible warnings."
    )


def _watch_label_and_remember(kind: str, watch_number: int | None) -> tuple[str, str, str]:
    """Return (watch_label, numbered_label, remember_text) for TO.A/SV.A watches."""
    watch_label = "Tornado Watch" if kind == "tornado" else "Severe Thunderstorm Watch"
    label_with_num = f"{watch_label} Number {watch_number}" if watch_number is not None else watch_label
    return watch_label, label_with_num, build_watch_reminder(kind)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _watch_section_script_lines(
    *,
    parsed: dict[str, Any],
    area_desc: str,
    local_tz: dt.tzinfo | None = None,
    now: dt.datetime | None = None,
) -> list[str]:
    """Build one clean WCN lifecycle statement from one VTEC-bearing WCN section."""
    action = str(parsed.get("action") or "").upper()
    _watch_label, label_with_num, _remember = _watch_label_and_remember(
        str(parsed.get("kind") or "severe"),
        parsed.get("watch_number"),
    )
    until = _watch_time_phrase(parsed.get("end_utc"), local_tz=local_tz, now=now)
    until_part = f" until {until}" if until else ""
    area_phrase = _watch_lifecycle_area_phrase(area_desc)

    def with_area(prefix: str) -> str:
        return f"{prefix} for {area_phrase}." if area_phrase else f"{prefix}."

    if action == "CAN":
        return [with_area(f"{label_with_num} has been cancelled")]
    if action == "EXP":
        return [with_area(f"{label_with_num} has been allowed to expire")]
    if action == "CON":
        return [with_area(f"{label_with_num} remains in effect{until_part}")]
    if action == "EXT":
        return [with_area(f"{label_with_num} is now in effect{until_part}")]
    if action == "EXA":
        if area_phrase:
            return [f"{label_with_num} remains in effect{until_part}, and now includes {area_phrase}."]
        return [f"{label_with_num} remains in effect{until_part}, and now includes additional areas."]
    if action == "EXB":
        if area_phrase:
            return [f"{label_with_num} is now in effect{until_part}, and now includes {area_phrase}."]
        return [f"{label_with_num} is now in effect{until_part}, and now includes additional areas."]
    return []

# --- centralized from seasonalweather/broadcast/product_text.py ---
def build_nwws_watch_partial_cancel_script(
    official_text: str,
    vtec: list[str] | None,
    *,
    local_tz: dt.tzinfo | None = None,
    now: dt.datetime | None = None,
) -> str:
    """
    Build clean narration for mixed-action WCN products, such as CAN+CON.

    Generic NWWS partial-cancel parsing expects warning/SVS-style segments with
    ``...headline...`` markers.  WCN products usually do not have those markers,
    so feeding WCN text to that parser can read WMO headers, UGC lines, county
    columns, and all-caps product prose.  This parser keeps WCN on the watch
    script path and formats each VTEC section with watch-specific wording.
    """
    parsed_sections: list[tuple[dict[str, Any], str]] = []
    for section in _split_nwws_vtec_sections(official_text):
        sec_vtecs = [m.group(0) for m in _WATCH_VTEC_RE.finditer(section)]
        parsed = _parse_watch_vtec(sec_vtecs)
        if not parsed:
            continue
        action = str(parsed.get("action") or "").upper()
        if action not in {"CAN", "EXP", "CON", "EXT", "EXA", "EXB"}:
            continue
        parsed_sections.append((parsed, _extract_wcn_area_desc(section)))

    if not parsed_sections:
        return ""

    actions = {str(parsed.get("action") or "").upper() for parsed, _area in parsed_sections}
    if not (actions & {"CAN", "EXP"} and actions & {"CON", "EXT", "EXA", "EXB"}):
        return ""

    lines: list[str] = []
    for parsed, area_desc in parsed_sections:
        # Do not create duplicate bare continuation lines for sections where we
        # could not recover any speakable area text, e.g. adjacent coastal-water
        # tails outside the configured SAME service-area context.
        if not area_desc and lines:
            continue
        lines.extend(
            _watch_section_script_lines(
                parsed=parsed,
                area_desc=area_desc,
                local_tz=local_tz,
                now=now,
            )
        )

    if not lines:
        return ""
    _watch_label, _label_with_num, remember = _watch_label_and_remember(
        str(parsed_sections[0][0].get("kind") or "severe"),
        parsed_sections[0][0].get("watch_number"),
    )
    if remember:
        lines.append(remember)
    lines.append(
        "Stay tuned to NOAA Weather Radio, commercial radio, and television outlets, "
        "or internet sources for the latest severe weather information."
    )
    return "\n\n".join(ln.strip() for ln in lines if ln and ln.strip()).strip()

# --- centralized from seasonalweather/broadcast/product_text.py ---
def build_nwws_watch_vtec_script(
    official_text: str,
    vtec: list[str] | None,
    *,
    local_tz: dt.tzinfo | None = None,
    area_text: str = "",
    now: dt.datetime | None = None,
    action: str | None = None,
) -> str:
    """
    Build NWR-style narration for NWWS WCN products carrying TO.A/SV.A VTEC.

    This is the NWWS-side equivalent of the CAP watch formatter. It prevents
    watch county notifications from being spoken as raw all-caps product text.
    """
    parsed = _parse_watch_vtec(vtec, action=action)
    if not parsed:
        return ""

    kind = parsed["kind"]
    action = str(parsed.get("action") or "").upper()
    watch_number = parsed.get("watch_number")
    until = _watch_time_phrase(parsed.get("end_utc"), local_tz=local_tz, now=now)
    area_desc = (area_text or "").strip() or _extract_wcn_area_desc(official_text)
    area_sentence = _watch_area_sentence(area_desc)

    _watch_label, label_with_num, remember = _watch_label_and_remember(kind, watch_number)
    until_part = f" until {until}" if until else ""

    lines: list[str] = []
    if action in {"CAN", "EXP", "CON", "EXT", "EXA", "EXB"}:
        lines.extend(
            _watch_section_script_lines(
                parsed=parsed,
                area_desc=area_desc,
                local_tz=local_tz,
                now=now,
            )
        )
    else:
        lines.append(f"The National Weather Service has issued {label_with_num}.")
        if until:
            lines.append(f"Effective until {until}.")
        if area_sentence:
            lines.append(area_sentence)

    # Terminal WCN updates end the watch for every section represented by this
    # product.  The watch-definition reminder is useful only while at least one
    # section remains active; reading it after a complete CAN/EXP is misleading.
    watch_actions = {
        m.group("action").upper()
        for raw in (vtec or [])
        if (m := _WATCH_VTEC_RE.search(str(raw).strip().upper()))
        and m.group("sig") == "A"
        and m.group("phen") in {"TO", "SV"}
    }
    has_active_section = bool(watch_actions & {"NEW", "CON", "EXT", "EXA", "EXB"})
    terminal_only = bool(watch_actions) and watch_actions <= {"CAN", "EXP"}
    if remember and (has_active_section or not terminal_only):
        lines.append(remember)

    lines.append(
        "Stay tuned to NOAA Weather Radio, commercial radio, and television outlets, "
        "or internet sources for the latest severe weather information."
    )
    return "\n\n".join(ln.strip() for ln in lines if ln and ln.strip()).strip()

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _wcn_action_sections(
    official_text: str,
    wanted_action: str,
) -> list[tuple[dict[str, Any], str]]:
    sections: list[tuple[dict[str, Any], str]] = []
    for section in _split_nwws_vtec_sections(official_text):
        section_vtec = [match.group(0) for match in _WATCH_VTEC_RE.finditer(section)]
        parsed = _parse_watch_vtec(section_vtec, action=wanted_action)
        if not parsed:
            continue
        section_actions = {match.group("action").upper() for match in _WATCH_VTEC_RE.finditer(section)}
        area_desc = _extract_wcn_area_desc(section)
        if wanted_action in {"CAN", "EXP"} and section_actions & {"NEW", "UPG", "EXA", "EXB"}:
            area_desc = ""
        sections.append((parsed, area_desc))
    return sections

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _wcn_action_lines(
    sections: list[tuple[dict[str, Any], str]],
    wanted_action: str,
    *,
    area_text: str,
    local_tz: dt.tzinfo | None,
    now: dt.datetime | None,
) -> list[str]:
    first = sections[0][0]
    kind = str(first.get("kind") or "severe")
    watch_number = first.get("watch_number")
    _watch_label, label_with_num, _remember = _watch_label_and_remember(kind, watch_number)
    until = _watch_time_phrase(first.get("end_utc"), local_tz=local_tz, now=now)
    if wanted_action == "NEW":
        return _wcn_new_action_lines(sections, area_text=area_text, label_with_num=label_with_num, until=until)
    return _wcn_lifecycle_action_lines(sections, area_text=area_text, local_tz=local_tz, now=now)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _wcn_new_action_lines(
    sections: list[tuple[dict[str, Any], str]],
    *,
    area_text: str,
    label_with_num: str,
    until: str,
) -> list[str]:
    lines = [f"The National Weather Service has issued {label_with_num}."]
    if until:
        lines.append(f"Effective until {until}.")
    for _parsed, section_area in sections:
        area_sentence = _watch_area_sentence(section_area or area_text)
        if area_sentence and area_sentence not in lines:
            lines.append(area_sentence)
    return lines

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _wcn_lifecycle_action_lines(
    sections: list[tuple[dict[str, Any], str]],
    *,
    area_text: str,
    local_tz: dt.tzinfo | None,
    now: dt.datetime | None,
) -> list[str]:
    lines: list[str] = []
    for parsed, section_area in sections:
        area = section_area or area_text
        if not area and lines:
            continue
        lines.extend(
            _watch_section_script_lines(
                parsed=parsed,
                area_desc=area,
                local_tz=local_tz,
                now=now,
            )
        )
    return lines

# --- centralized from seasonalweather/broadcast/product_text.py ---
def build_nwws_watch_action_script(
    official_text: str,
    vtec: list[str] | None,
    action: str,
    *,
    local_tz: dt.tzinfo | None = None,
    area_text: str = "",
    now: dt.datetime | None = None,
) -> str:
    """Render only the WCN sections carrying one lifecycle action.

    A WCN can carry a terminal action for one watch footprint and a NEW or
    continuation action for another footprint in the same product.  Keeping
    those sections separate lets the runtime put the active action in the FULL
    cut and the terminal action in a following VOICE cut.
    """
    wanted_action = (action or "").strip().upper()
    if not wanted_action:
        return ""

    sections = _wcn_action_sections(official_text, wanted_action)

    if not sections:
        return build_nwws_watch_vtec_script(
            official_text,
            vtec,
            local_tz=local_tz,
            area_text=area_text,
            now=now,
            action=wanted_action,
        )

    lines = _wcn_action_lines(
        sections,
        wanted_action,
        area_text=area_text,
        local_tz=local_tz,
        now=now,
    )

    if not lines:
        return ""
    return _finish_wcn_action_script(sections, wanted_action, lines)

# --- centralized from seasonalweather/broadcast/product_text.py ---
def _finish_wcn_action_script(
    sections: list[tuple[dict[str, Any], str]],
    wanted_action: str,
    lines: list[str],
) -> str:
    first = sections[0][0]
    kind = str(first.get("kind") or "severe")
    watch_number = first.get("watch_number")
    _watch_label, _label_with_num, remember = _watch_label_and_remember(kind, watch_number)
    if wanted_action not in {"CAN", "EXP"} and remember:
        lines.append(remember)
    lines.append(
        "Stay tuned to NOAA Weather Radio, commercial radio, and television outlets, "
        "or internet sources for the latest severe weather information."
    )
    return "\n\n".join(ln.strip() for ln in lines if ln and ln.strip()).strip()

# --- centralized from seasonalweather/broadcast/product_text.py ---
@dataclass(frozen=True)
class NwwsScriptRenderResult:
    script: str
    changed: bool = False
    renderer: str = "base"
    notes: tuple[str, ...] = ()

# --- centralized from seasonalweather/broadcast/product_text.py ---
def build_nwws_statement_vtec_action_script(
    *,
    event_text: str,
    area_text: str,
    official_text: str,
    headline: str,
    vtec_actions: set[str],
) -> str:
    """Build the lighter NWWS statement/advisory/message EXP/CAN narration."""
    return build_statement_vtec_action_script(
        event=event_text,
        area_desc=area_text or _extract_county_area_text(official_text) or "",
        description=official_text,
        headline=headline,
        vtec=[],
        vtec_actions=vtec_actions,
        parameters=None,
        sps_preamble=lambda sent_iso=None: sps_preamble(sent_iso),
    )

# --- centralized from seasonalweather/broadcast/product_text.py ---
def render_nws_product_script(
    *,
    product_type: str,
    base_script: str,
    official_text: str,
    vtec: list[str],
    vtec_actions: set[str],
    has_tracks: bool,
    should_full: bool,
    event_text: str,
    area_text: str,
    headline: str,
    local_tz: dt.tzinfo | None = None,
    watch_action: str | None = None,
) -> NwwsScriptRenderResult:
    """
    Normalize NWS spoken scripts after the generic alert builder.

    This central formatter owns product-specific narration overrides for NWS
    products regardless of transport: WCN watch wording, SPS preambles, partial
    cancels, terminal cancels/expirations, and statement-style CAN/EXP narration.
    The historical NWWS name remains as a compatibility alias below.
    """
    ptype = (product_type or "").strip().upper()
    script = base_script or ""
    changed = False
    renderer = "base"
    notes: list[str] = []

    if watch_action:
        watch_script = build_nwws_watch_action_script(
            official_text,
            vtec,
            watch_action,
            local_tz=local_tz,
            area_text=area_text,
        )
    else:
        watch_script = build_nwws_watch_vtec_script(
            official_text,
            vtec,
            local_tz=local_tz,
            area_text=area_text,
        )
    if watch_script:
        script = watch_script
        changed = True
        renderer = "nwws-watch-vtec"

    if watch_action and watch_script and ptype == "WCN":
        return NwwsScriptRenderResult(
            script=script,
            changed=changed,
            renderer=renderer,
            notes=tuple(notes),
        )

    if ptype == "SPS":
        fixed = fix_sps_preamble(script, official_text)
        if fixed.strip() != script.strip():
            script = fixed
            changed = True
            renderer = "nwws-sps-preamble"

    if not (has_tracks and not should_full and ({"EXP", "CAN"} & vtec_actions)):
        return NwwsScriptRenderResult(
            script=script,
            changed=changed,
            renderer=renderer,
            notes=tuple(notes),
        )

    has_continuation = bool(vtec_actions & {"CON", "EXT", "EXA", "EXB"})

    if has_continuation:
        if ptype == "WCN":
            watch_partial_script = build_nwws_watch_partial_cancel_script(
                official_text,
                vtec,
                local_tz=local_tz,
            )
            if watch_partial_script:
                return NwwsScriptRenderResult(
                    script=watch_partial_script,
                    changed=True,
                    renderer="nwws-watch-partial-cancel",
                    notes=tuple(notes),
                )
            notes.append("warning: WCN watch partial-cancel parser returned no script; preserving prior script")
            return NwwsScriptRenderResult(
                script=script,
                changed=changed,
                renderer=renderer,
                notes=tuple(notes),
            )

        segments = parse_nwws_product_segments(official_text)
        partial_script = build_nwws_partial_cancel_script(event_text, segments)
        if partial_script:
            return NwwsScriptRenderResult(
                script=partial_script,
                changed=True,
                renderer="nwws-partial-cancel",
                notes=tuple([*notes, f"segments={len(segments)}"]),
            )
        notes.append("warning: partial-cancel segment parser returned no script; preserving prior script")
        return NwwsScriptRenderResult(
            script=script,
            changed=changed,
            renderer=renderer,
            notes=tuple(notes),
        )

    if cap_prefers_statement_update_script(event_text, vtec_actions):
        return NwwsScriptRenderResult(
            script=build_nwws_statement_vtec_action_script(
                event_text=event_text,
                area_text=area_text,
                official_text=official_text,
                headline=headline,
                vtec_actions=vtec_actions,
            ),
            changed=True,
            renderer="nwws-statement-vtec-action",
            notes=tuple(notes),
        )

    detailed_terminal_script = ""
    if ptype in {"FLS", "FFS", "SVS"}:
        detailed_terminal_script = build_nwws_terminal_cancel_expiry_script(
            event_text,
            official_text,
        )

    if detailed_terminal_script:
        return NwwsScriptRenderResult(
            script=detailed_terminal_script,
            changed=True,
            renderer="nwws-detailed-terminal-cancel-expiry",
            notes=tuple(notes),
        )

    summary = expiry_summary_script(official_text)
    if summary:
        return NwwsScriptRenderResult(
            script=summary,
            changed=True,
            renderer="nwws-terminal-summary",
            notes=tuple(notes),
        )

    return NwwsScriptRenderResult(
        script=script,
        changed=changed,
        renderer=renderer,
        notes=tuple(notes),
    )

# --- centralized from seasonalweather/broadcast/product_text.py ---
def render_nwws_product_script(**kwargs) -> NwwsScriptRenderResult:
    """Compatibility wrapper for the central NWS product formatter."""
    return render_nws_product_script(**kwargs)

# --- centralized from seasonalweather/broadcast/product_text.py ---
__all__ = [
    # Constants
    "STATE_NAME_FULL",
    # Text utilities
    "clean_cap_text",
    "expand_tz_token",
    "fix_sps_preamble",
    "fmt_local_from_utc_iso",
    "join_oxford",
    "nws_header_issued_phrase",
    "parse_cap_area_by_state",
    "sps_preamble",
    # Central NWS alert text model
    "NwsAlertTextInput",
    "build_nws_full_alert_script",
    "build_nws_voice_alert_script",
    # CAP helpers (pre-existing)
    "cap_area_label",
    "cap_expiry_summary_line",
    "cap_full_opening_line",
    "cap_is_special_weather_statement",
    "cap_normalize_nws_headline",
    "cap_nwsheadline",
    "cap_prefers_statement_update_script",
    "cap_statement_area_noun",
    "cap_statement_intro",
    "cap_uses_sps_preamble",
    # Script builders
    "build_statement_vtec_action_script",
    "build_warning_vtec_action_script",
    "build_nwws_statement_vtec_action_script",
    # NWWS helpers
    "expiry_summary_script",
    "NwwsProductSegment",
    "parse_nwws_product_segments",
    "build_nwws_partial_cancel_script",
    "build_nwws_terminal_cancel_expiry_script",
    "extract_nwws_wcn_area_desc",
    "match_nwws_wcn_area_same",
    "build_nwws_watch_vtec_script",
    "build_nwws_watch_action_script",
    "build_nwws_watch_partial_cancel_script",
    "NwwsScriptRenderResult",
    "render_nws_product_script",
    "render_nwws_product_script",
]

# cap_text.py's former cross-module aliases now bind directly to this one
# implementation namespace.
_STATE_NAME_FULL_MAP = STATE_NAME_FULL
_build_statement_vtec_action_script_fn = build_statement_vtec_action_script
_build_warning_vtec_action_script_fn = build_warning_vtec_action_script
_cap_expiry_summary_line = cap_expiry_summary_line
_fmt_local_from_utc_iso = fmt_local_from_utc_iso
_cap_prefers_statement_update_script_fn = cap_prefers_statement_update_script
_build_nws_full_alert_script = build_nws_full_alert_script
_build_nws_voice_alert_script = build_nws_voice_alert_script
_pt_clean_cap_text = clean_cap_text
_pt_join_oxford = join_oxford
_pt_parse_cap_area_by_state = parse_cap_area_by_state
_pt_nws_header_issued_phrase = nws_header_issued_phrase
_pt_sps_preamble = sps_preamble

# --- centralized from seasonalweather/broadcast/cap_text.py ---
class CapTextRenderer:
    _STATE_NAME_FULL: dict[str, str] = _STATE_NAME_FULL_MAP

    def __init__(
        self,
        *,
        local_tz: ZoneInfo,
        cap_vtec_list: Callable[[object], list[str]],
        vtec_tracks: Callable[[list[str]], list[tuple[str, str]]],
        best_expiry_from_vtec: Callable[[list[str]], dt.datetime | None],
    ) -> None:
        self._tz: ZoneInfo = local_tz
        self._cap_vtec_list: Callable[[object], list[str]] = cap_vtec_list
        self._vtec_tracks: Callable[[list[str]], list[tuple[str, str]]] = vtec_tracks
        self._best_expiry_from_vtec: Callable[[list[str]], dt.datetime | None] = best_expiry_from_vtec

    def _nws_header_issued_phrase(self, text: str) -> str | None:
        return _pt_nws_header_issued_phrase(text)

    def _cap_sps_preamble(self, sent_iso: str | None) -> str:
        return _pt_sps_preamble(sent_iso, local_tz=self._tz)

    def _clean_cap_text(self, s: str, *, limit: int = 900) -> str:
        """Shim → product_text.clean_cap_text()."""
        return _pt_clean_cap_text(s, limit=limit)

    def _build_cap_watch_script(self, ev: CapAlertEvent) -> str:
        """
        Build a sane, NWR-style script for CAP Tornado Watch / Severe Thunderstorm Watch.
        Returns "" if this CAP event is not a watch.

        Why: CAP watch descriptions are often all-caps blobs with little punctuation,
        which TTS will speed-read. NWR uses a standardized narration instead.
        """
        # ---- Determine watch kind (prefer event label, fall back to VTEC) ----
        kind: str | None = None  # "tornado" or "severe"
        ev_name = (getattr(ev, "event", "") or "").strip().lower()

        if ev_name == "tornado watch":
            kind = "tornado"
        elif ev_name == "severe thunderstorm watch":
            kind = "severe"
        else:
            # Fall back to VTEC phen/sig
            for v in self._cap_vtec_list(ev):
                m = _VTEC_PARSE_RE.search(v)
                if not m:
                    continue
                phen = (m.group("phen") or "").upper()
                sig = (m.group("sig") or "").upper()
                if sig != "A":
                    continue
                if phen == "TO":
                    kind = "tornado"
                    break
                if phen == "SV":
                    kind = "severe"
                    break

        if not kind:
            return ""

        # ---- Helpers ----
        def _parse_vtec_z(tok: str):
            # tok like YYYYMMDDT0000Z or YYMMDDT0000Z
            s = (tok or "").strip().upper()
            mm = re.fullmatch(r"(\d{8}|\d{6})T(\d{4})Z", s)
            if not mm:
                return None
            d = mm.group(1)
            hm = mm.group(2)
            if len(d) == 8:
                year = int(d[0:4]); month = int(d[4:6]); day = int(d[6:8])
            else:
                year = 2000 + int(d[0:2]); month = int(d[2:4]); day = int(d[4:6])
            hour = int(hm[0:2]); minute = int(hm[2:4])
            try:
                return dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc)
            except Exception:
                return None

        def _fmt_time_local(d: dt.datetime) -> str:
            # "8 PM" or "8:30 PM"
            hour12 = d.hour % 12
            if hour12 == 0:
                hour12 = 12
            ampm = "AM" if d.hour < 12 else "PM"
            if d.minute == 0:
                return f"{hour12} {ampm}"
            return f"{hour12}:{d.minute:02d} {ampm}"

        def _daypart(d: dt.datetime) -> str:
            # rough-but-good NWR-ish phrasing
            if d.hour < 12:
                return "morning"
            if d.hour < 17:
                return "afternoon"
            if d.hour < 21:
                return "evening"
            return "tonight"

        def _until_phrase(end_local: dt.datetime) -> str:
            now_local = dt.datetime.now(tz=self._tz)
            t = _fmt_time_local(end_local)
            dp = _daypart(end_local)

            if end_local.date() == now_local.date():
                if dp == "tonight":
                    return f"until {t} tonight"
                return f"until {t} this {dp}"

            if (end_local.date() - now_local.date()).days == 1:
                if dp == "tonight":
                    return f"until {t} tomorrow night"
                return f"until {t} tomorrow {dp}"

            # fallback: weekday
            wd = end_local.strftime("%A")
            return f"until {t} on {wd}"

        def _join_oxford(items: list[str]) -> str:
            xs = [x.strip() for x in items if x and x.strip()]
            if not xs:
                return ""
            if len(xs) == 1:
                return xs[0]
            if len(xs) == 2:
                return f"{xs[0]} and {xs[1]}"
            return ", ".join(xs[:-1]) + f", and {xs[-1]}"

        STATE_NAME = {
            "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado","CT":"Connecticut",
            "DE":"Delaware","DC":"the District of Columbia","FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho","IL":"Illinois",
            "IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland","MA":"Massachusetts",
            "MI":"Michigan","MN":"Minnesota","MS":"Mississippi","MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire",
            "NJ":"New Jersey","NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon",
            "PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont",
            "VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming",
        }

        # ---- Extract watch number + end time from VTEC ----
        watch_num: int | None = None
        end_utc: dt.datetime | None = None

        for v in self._cap_vtec_list(ev):
            m = _VTEC_PARSE_RE.search(v)
            if not m:
                continue
            phen = (m.group("phen") or "").upper()
            sig = (m.group("sig") or "").upper()
            if sig != "A":
                continue
            if kind == "tornado" and phen != "TO":
                continue
            if kind == "severe" and phen != "SV":
                continue

            try:
                watch_num = int(m.group("etn"))
            except Exception:
                watch_num = None

            end_utc = _parse_vtec_z(m.group("end") or "")
            break

        end_phrase = ""
        if end_utc is not None:
            end_local = end_utc.astimezone(self._tz)
            end_phrase = _until_phrase(end_local)

        # ---- Parse counties/states from CAP areaDesc ----
        area_desc = (getattr(ev, "area_desc", "") or "").strip()
        # CAP areaDesc often: "Cambria, PA; Cameron, PA; ..."
        groups: dict[str, list[str]] = {}
        order: list[str] = []
        misc: list[str] = []

        for raw in re.split(r";\s*", area_desc):
            s = (raw or "").strip().strip(".")
            if not s:
                continue
            if "," in s:
                name, st = s.rsplit(",", 1)
                name = name.strip()
                st = st.strip().upper()
                if st not in groups:
                    groups[st] = []
                    order.append(st)
                groups[st].append(name)
            else:
                misc.append(s)

        # ---- Boilerplate ----
        watch_label, _label_with_num, remember = _watch_label_and_remember(kind, watch_num)

        stay_tuned = (
            "Stay tuned to NOAA Weather Radio, commercial radio, and television outlets, "
            "or internet sources for the latest severe weather information."
        )

        # ---- Build script ----
        lines: list[str] = []

        if watch_num is not None:
            lines.append(f"The National Weather Service has issued {watch_label} Number {watch_num}.")
        else:
            lines.append(f"The National Weather Service has issued {watch_label}.")

        if end_phrase:
            lines.append(f"Effective {end_phrase}.")

        if groups:
            if len(order) == 1:
                st = order[0]
                st_full = STATE_NAME.get(st, st)
                county_list = _join_oxford(groups.get(st, []))
                if county_list:
                    lines.append(f"This watch includes the following counties, in {st_full}: {county_list}.")
            else:
                segs: list[str] = []
                for st in order:
                    st_full = STATE_NAME.get(st, st)
                    county_list = _join_oxford(groups.get(st, []))
                    if county_list:
                        segs.append(f"in {st_full}: {county_list}")
                if segs:
                    lines.append("This watch includes the following counties: " + "; ".join(segs) + ".")
        elif area_desc:
            # fallback if parsing fails
            lines.append(f"This watch includes the following areas: {area_desc}.")

        # If CAP areaDesc was empty but we have leftovers
        if misc and not groups:
            lines.append("This watch includes: " + _join_oxford(misc) + ".")

        lines.append(remember)
        lines.append(stay_tuned)

        # Double-newlines => better pacing
        return "\n\n".join(ln.strip() for ln in lines if ln and ln.strip()).strip()

    def _parse_cap_area_by_state(self, area_desc: str) -> tuple[dict[str, list[str]], list[str], list[str]]:
        """Shim → product_text.parse_cap_area_by_state()."""
        return _pt_parse_cap_area_by_state(area_desc)

    def _join_oxford(self, items: list[str]) -> str:
        """Shim → product_text.join_oxford()."""
        return _pt_join_oxford(items)

    def _fmt_local_from_utc_iso(self, iso_str: str) -> str:
        return _fmt_local_from_utc_iso(iso_str, local_tz=self._tz)

    def _cap_prefers_statement_update_script(self, event: str, vtec_actions: set[str]) -> bool:
        """Shim → product_text.cap_prefers_statement_update_script()."""
        return _cap_prefers_statement_update_script_fn(event, vtec_actions)

    def _cap_expiry_summary_line(self, text: str) -> str:
        """Shim → product_text.cap_expiry_summary_line()."""
        return _cap_expiry_summary_line(text)

    def _build_statement_vtec_action_script(
        self,
        ev: CapAlertEvent,
        vtec_actions: set[str],
        tracks: list[tuple[str, str]],
    ) -> str:
        """Shim → product_text.build_statement_vtec_action_script()."""
        return _build_statement_vtec_action_script_fn(
            event=getattr(ev, "event", "") or "",
            area_desc=(getattr(ev, "area_desc", "") or "").strip(),
            description=str(getattr(ev, "description", "") or "").strip(),
            headline=str(getattr(ev, "headline", "") or "").strip(),
            vtec=self._cap_vtec_list(ev),
            vtec_actions=vtec_actions,
            parameters=getattr(ev, "parameters", {}) or {},
            sps_preamble=self._cap_sps_preamble,
            sent_iso=getattr(ev, "sent", None),
        )

    def _build_warning_vtec_action_script(
        self,
        ev: CapAlertEvent,
        vtec_actions: set[str],
        tracks: list[tuple[str, str]],
    ) -> str:
        """Shim → product_text.build_warning_vtec_action_script()."""
        vtec = self._cap_vtec_list(ev)
        exp_utc = self._best_expiry_from_vtec(vtec)
        exp_phrase = ""
        if exp_utc:
            exp_phrase = self._fmt_local_from_utc_iso(exp_utc.isoformat())
        if not exp_phrase:
            raw_exp = getattr(ev, "expires", None)
            if raw_exp:
                exp_phrase = self._fmt_local_from_utc_iso(str(raw_exp))

        result = _build_warning_vtec_action_script_fn(
            event=getattr(ev, "event", "") or "",
            headline=getattr(ev, "headline", "") or "",
            description=str(getattr(ev, "description", "") or ""),
            instruction=str(getattr(ev, "instruction", "") or ""),
            area_desc=(getattr(ev, "area_desc", "") or "").strip(),
            vtec_actions=vtec_actions,
            exp_phrase=exp_phrase,
        )
        # If the free function produced nothing, fall through to the full script.
        if not result:
            return self._build_cap_full_script(ev)
        return result

    def _build_watch_vtec_action_script(
        self,
        ev: CapAlertEvent,
        vtec_actions: set[str],
        tracks: list[tuple[str, str]],
        watch_number: int | None,
        kind: str,  # "tornado" or "severe"
    ) -> str:
        """
        NWR-style voice script for VTEC update/cancel actions on watches (TOA/SVA).

        CON      → "Watch Number N remains in effect until …"
        EXA      → "Watch Number N remains in effect until … and now includes …"
        CAN      → "Watch Number N has been cancelled for … in …"
        EXP      → "Watch Number N has been allowed to expire for … in …"
        """
        watch_label = "Tornado Watch" if kind == "tornado" else "Severe Thunderstorm Watch"
        num_phrase = f"Number {watch_number}" if watch_number is not None else ""
        label_with_num = f"{watch_label} {num_phrase}".strip()

        area_desc = (getattr(ev, "area_desc", "") or "").strip()
        groups, order, misc = self._parse_cap_area_by_state(area_desc)

        vtec = self._cap_vtec_list(ev)
        exp_utc = self._best_expiry_from_vtec(vtec)
        exp_phrase = ""
        if exp_utc:
            exp_phrase = self._fmt_local_from_utc_iso(exp_utc.isoformat())
        if not exp_phrase:
            raw_exp = getattr(ev, "expires", None)
            if raw_exp:
                exp_phrase = self._fmt_local_from_utc_iso(str(raw_exp))

        def _county_segs() -> str:
            """Build 'in Maryland: Allegany, Garrett' style phrase."""
            if not groups:
                return area_desc or "the affected areas"
            parts: list[str] = []
            for st in order:
                st_full = self._STATE_NAME_FULL.get(st, st)
                county_list = self._join_oxford(groups[st])
                if county_list:
                    parts.append(f"in {st_full}: {county_list}")
            if parts:
                return "; ".join(parts)
            return area_desc or "the affected areas"

        lines: list[str] = []

        if vtec_actions & {"CAN"}:
            lines.append(f"{label_with_num} has been cancelled for the following areas.")
            lines.append(_county_segs() + ".")

        elif vtec_actions & {"EXP"}:
            lines.append(f"{label_with_num} has been allowed to expire for the following areas.")
            lines.append(_county_segs() + ".")

        elif vtec_actions & {"EXA", "EXB"}:
            # Watch expansion — also used when area grows mid-event
            lines.append(f"{label_with_num} remains in effect" + (f" until {exp_phrase}" if exp_phrase else "") + ".")
            lines.append("This watch now includes the following additional areas.")
            lines.append(_county_segs() + ".")

        else:  # CON / EXT
            lines.append(f"{label_with_num} remains in effect" + (f" until {exp_phrase}" if exp_phrase else "") + ".")
            lines.append(f"This watch includes the following areas: {_county_segs()}.")

        if not lines:
            return self._build_cap_watch_script(ev)

        lines.append("Stay tuned to NOAA Weather Radio, commercial radio, and television outlets for the latest severe weather information.")
        return "\n".join(ln.strip() for ln in lines if ln and ln.strip()).strip()

    def _build_watch_expansion_script(self, ev: CapAlertEvent) -> str:
        """
        Full NWR-style script for watch EXA/EXB: new SAME tones, full county listing.
        Expansion is treated as a new issuance for the added counties.
        """
        # Determine kind + watch number from VTEC
        kind = "tornado"
        watch_number: int | None = None
        for v in self._cap_vtec_list(ev):
            m = _VTEC_PARSE_RE.search(v)
            if not m:
                continue
            phen = (m.group("phen") or "").upper()
            sig = (m.group("sig") or "").upper()
            if sig != "A":
                continue
            if phen == "TO":
                kind = "tornado"
            elif phen == "SV":
                kind = "severe"
            else:
                continue
            try:
                watch_number = int(m.group("etn"))
            except Exception:
                pass
            break

        tracks = self._vtec_tracks(self._cap_vtec_list(ev))
        return self._build_watch_vtec_action_script(
            ev,
            vtec_actions={"EXA"},
            tracks=tracks,
            watch_number=watch_number,
            kind=kind,
        )

    def _build_cap_full_script(self, ev: CapAlertEvent) -> str:
        """CAP adapter → central NWS full-alert formatter."""
        return _build_nws_full_alert_script(
            NwsAlertTextInput(
                event=str(getattr(ev, "event", "") or ""),
                headline=str(getattr(ev, "headline", "") or ""),
                description=str(getattr(ev, "description", "") or ""),
                instruction=str(getattr(ev, "instruction", "") or ""),
                area_desc=str(getattr(ev, "area_desc", "") or ""),
                sent_iso=getattr(ev, "sent", None),
                expires_iso=getattr(ev, "expires", None),
                parameters=getattr(ev, "parameters", {}) or {},
                vtec=self._cap_vtec_list(ev),
            ),
            sps_preamble=self._cap_sps_preamble,
        )

    def _build_cap_voice_script(self, ev: CapAlertEvent) -> str:
        """CAP adapter → central NWS voice/update formatter."""
        return _build_nws_voice_alert_script(
            NwsAlertTextInput(
                event=str(getattr(ev, "event", "") or ""),
                headline=str(getattr(ev, "headline", "") or ""),
                description=str(getattr(ev, "description", "") or ""),
                instruction=str(getattr(ev, "instruction", "") or ""),
                area_desc=str(getattr(ev, "area_desc", "") or ""),
                sent_iso=getattr(ev, "sent", None),
                expires_iso=getattr(ev, "expires", None),
                parameters=getattr(ev, "parameters", {}) or {},
                vtec=self._cap_vtec_list(ev),
            ),
            sps_preamble=self._cap_sps_preamble,
        )

    # Public subsystem ports.  The underscored methods above remain as
    # compatibility seams for focused tests and older callers; production
    # runtime code reaches them through FormatterSubsystem only.
    def build_watch_expansion_script(self, ev: CapAlertEvent) -> str:
        return self._build_watch_expansion_script(ev)

    def build_cap_watch_script(self, ev: CapAlertEvent) -> str:
        return self._build_cap_watch_script(ev)

    def build_cap_full_script(self, ev: CapAlertEvent) -> str:
        return self._build_cap_full_script(ev)

    def build_cap_voice_script(self, ev: CapAlertEvent) -> str:
        return self._build_cap_voice_script(ev)

    def build_watch_vtec_action_script(
        self,
        ev: CapAlertEvent,
        vtec_actions: set[str],
        tracks: list[tuple[str, str]],
        watch_number: int | None,
        kind: str,
    ) -> str:
        return self._build_watch_vtec_action_script(ev, vtec_actions, tracks, watch_number, kind)

    def cap_prefers_statement_update_script(self, ev: CapAlertEvent | str, vtec_actions: set[str]) -> bool:
        event = ev if isinstance(ev, str) else str(getattr(ev, "event", "") or "")
        return self._cap_prefers_statement_update_script(event, vtec_actions)

    def build_statement_vtec_action_script(
        self,
        ev: CapAlertEvent,
        vtec_actions: set[str],
        tracks: list[tuple[str, str]],
    ) -> str:
        return self._build_statement_vtec_action_script(ev, vtec_actions, tracks)

    def build_warning_vtec_action_script(
        self,
        ev: CapAlertEvent,
        vtec_actions: set[str],
        tracks: list[tuple[str, str]],
    ) -> str:
        return self._build_warning_vtec_action_script(ev, vtec_actions, tracks)

    def fmt_local_from_utc_iso(self, iso_str: str) -> str:
        return self._fmt_local_from_utc_iso(iso_str)

# --- centralized from seasonalweather/broadcast/ern_script.py ---
_TEST_EVENT_CODES = {"DMO", "NAT", "NPT", "NST", "RMT", "RWT"}

# --- centralized from seasonalweather/broadcast/ern_script.py ---
def _article(word: str) -> str:
    w = str(word or "").strip()
    return "an" if w[:1].lower() in {"a", "e", "i", "o", "u"} else "a"

# --- centralized from seasonalweather/broadcast/ern_script.py ---
def _sentence(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    return s if s.endswith((".", "!", "?")) else s + "."

# --- centralized from seasonalweather/broadcast/ern_script.py ---
def _join_human(items: Sequence[str]) -> str:
    values: list[str] = []
    seen: set[str] = set()
    for raw in items or []:
        s = str(raw or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        values.append(s)
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return ", ".join(values[:-1]) + f", and {values[-1]}"

# --- centralized from seasonalweather/broadcast/ern_script.py ---
def _parse_duration_minutes(tttt: str | None) -> int | None:
    raw = str(tttt or "").strip()
    if len(raw) != 4 or not raw.isdigit():
        return None
    hours = int(raw[:2])
    minutes = int(raw[2:])
    if minutes > 59:
        return None
    return hours * 60 + minutes

# --- centralized from seasonalweather/broadcast/ern_script.py ---
def _same_jday_to_utc(jjjhhmm: str | None, *, now_utc: dt.datetime | None = None) -> dt.datetime | None:
    raw = str(jjjhhmm or "").strip()
    if len(raw) != 7 or not raw.isdigit():
        return None

    now = now_utc or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)

    jday = int(raw[:3])
    hour = int(raw[3:5])
    minute = int(raw[5:7])
    if jday < 1 or jday > 366 or hour > 23 or minute > 59:
        return None

    candidates: list[dt.datetime] = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            base = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc)
            candidates.append(base + dt.timedelta(days=jday - 1, hours=hour, minutes=minute))
        except Exception:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: abs((candidate - now).total_seconds()))

# --- centralized from seasonalweather/broadcast/ern_script.py ---
def _fmt_when(value: dt.datetime, *, tz: dt.tzinfo | None = None) -> str:
    target = value
    if target.tzinfo is None:
        target = target.replace(tzinfo=dt.timezone.utc)
    if tz is not None:
        target = target.astimezone(tz)
    else:
        target = target.astimezone()
    try:
        return target.strftime("%-I:%M %p %Z on %A, %B %-d")
    except Exception:
        return target.isoformat()

# --- centralized from seasonalweather/broadcast/ern_script.py ---
def parse_duration_minutes(tttt: str | None) -> int | None:
    """Public formatter-subsystem port for ERN duration parsing."""
    return _parse_duration_minutes(tttt)

# --- centralized from seasonalweather/broadcast/ern_script.py ---
def same_jday_to_utc(jjjhhmm: str | None, *, now_utc: dt.datetime | None = None) -> dt.datetime | None:
    """Public formatter-subsystem port for ERN start-time parsing."""
    return _same_jday_to_utc(jjjhhmm, now_utc=now_utc)

# --- centralized from seasonalweather/broadcast/ern_script.py ---
def build_ern_relay_script(
    ev: object,
    *,
    same_locations: Sequence[str] | None = None,
    area_text: str = "",
    tz: dt.tzinfo | None = None,
    now_utc: dt.datetime | None = None,
) -> str:
    """
    Build the spoken script for an ERN/GWES SAME relay.

    ERN/GWES gives SeasonalWeather decoded SAME metadata, not the full official
    alert body. This intentionally speaks the trustworthy header fields only:
    event, originator, area, effective/purge timing, and sender.
    """
    code = str(getattr(ev, "event", None) or "").strip().upper()
    event_label = label_or_code(code) if code else "EAS Alert"
    article = _article(event_label)
    sender = str(getattr(ev, "sender", None) or "").strip()
    org = str(getattr(ev, "org", None) or "").strip().upper()

    if code in _TEST_EVENT_CODES:
        intro = (
            f"The Emergency Relay Network reports {article} {event_label}. "
            "This is only a test."
        )
    else:
        intro = (
            f"The Emergency Relay Network reports {article} {event_label}. "
            "This relay will remain in the active alert rotation until it expires, "
            "or until authoritative CAP, NWWS, or IPAWS alert text supersedes it."
        )

    lines: list[str] = [intro]

    if org:
        lines.append(f"{org_broadcast_prefix(org)} {article} {event_label}.")

    area = str(area_text or "").strip()
    if area:
        lines.append(_sentence(f"The message applies to the following locations: {area}"))
    else:
        same_text = _join_human([str(x).strip() for x in (same_locations or []) if str(x).strip()])
        if same_text:
            lines.append(_sentence(f"The message applies to the following SAME locations: {same_text}"))

    start_utc = _same_jday_to_utc(getattr(ev, "jjjhhmm", None), now_utc=now_utc)
    duration_min = _parse_duration_minutes(getattr(ev, "tttt", None))
    if start_utc is not None and duration_min is not None:
        end_utc = start_utc + dt.timedelta(minutes=duration_min)
        lines.append(
            f"The message is valid from: {_fmt_when(start_utc, tz=tz)}. "
            f"And the message is valid until: {_fmt_when(end_utc, tz=tz)}."
        )
    elif start_utc is not None:
        lines.append(f"The message is valid from: {_fmt_when(start_utc, tz=tz)}.")

    if sender:
        lines.append(f"The message was received from: {sender}.")

    if code in _TEST_EVENT_CODES:
        lines.append("End of test message.")
    return "\n".join(line.strip() for line in lines if line and line.strip())

# --- centralized from seasonalweather/broadcast/ipaws_text.py ---
def build_ipaws_script(ev: Any) -> str:
    """
    Build a NWR-style TTS script for an IPAWS civil alert.

    Format mirrors how real NWR handles NWEMs:
      The following message is transmitted at the request of [authority].
      [headline if useful]
      [description]
      [instruction, if distinct]

    The authority line is omitted only when the cleaned senderName is
    unusable AND no area description is available to anchor it.
    """
    authority = (ev.sender_name_clean or "").strip()
    event = (ev.event or "").strip()
    headline = (ev.headline or "").strip()
    description = (ev.description or "").strip()
    instruction = (ev.instruction or "").strip()

    # Normalize whitespace/newlines that appear in IPAWS description fields.
    def _norm(s: str, limit: int = 900) -> str:
        s2 = re.sub(r"[\r\n]+", " ", s)
        s2 = re.sub(r"\s{2,}", " ", s2).strip()
        if len(s2) > limit:
            s2 = s2[:limit].rstrip() + "..."
        return s2

    description = _norm(description, 900)
    instruction = _norm(instruction, 600)
    headline = _norm(headline, 280)

    lines: list[str] = []

    # Preamble line.
    if authority:
        lines.append(
            f"The following message is transmitted at the request of {authority}."
        )
    else:
        # Fallback when senderName is generic or absent.
        lines.append("The following message is transmitted at the request of local authorities.")

    # Headline — only include when it adds information beyond the event name.
    # NWS-style "Civil Emergency Message" headlines are redundant; the real
    # content is in description.  But some senders write a useful summary
    # (e.g. "Tornado Watch in effect until 10pm for Worth County").
    hl_lower = headline.lower()
    ev_lower = event.lower()
    if headline and hl_lower != ev_lower and not hl_lower.startswith(ev_lower):
        lines.append(headline if headline.endswith((".", "!", "?")) else headline + ".")

    # Body.
    if description:
        lines.append(description)

    # Instruction — skip if it's a verbatim repeat of the description.
    if instruction and instruction.lower() != description.lower():
        lines.append("Instructions.")
        lines.append(instruction)

    return "\n".join(ln.strip() for ln in lines if ln.strip()).strip()

# --- centralized from seasonalweather/broadcast/now.py ---
_NOW_MARKER_RE = re.compile(r"^\s*\.NOW\.\.\.\s*$", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/now.py ---
_NOW_STOP_RE = re.compile(r"^\s*(?:&&|\$\$|NNNN)\s*$", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/now.py ---
_NOW_MACHINE_BLOCK_RE = re.compile(
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

# --- centralized from seasonalweather/broadcast/now.py ---
_LOCATIONS_INCLUDE_RE = re.compile(
    r"^(Locations?\s+(?:impacted|affected)\s+include)\.{3}\s*$",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/now.py ---
def extract_now_narrative(product_text: str) -> str:
    """Return only the human-readable body after the standard ``.NOW...`` marker.

    The extractor deliberately fails closed when the marker is absent.  Reading
    from an inferred offset risks sending routing headers, UGC codes, or other
    machine fields to TTS.  Terminal machine-readable blocks are discarded even
    when the office omits the usual ``&&`` delimiter.
    """
    lines = (product_text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()

    marker_index = next(
        (idx for idx, raw in enumerate(lines) if _NOW_MARKER_RE.match(raw)),
        None,
    )
    if marker_index is None:
        return ""

    paragraphs: list[str] = []
    current: list[str] = []

    def _flush() -> None:
        if not current:
            return
        paragraphs.append(" ".join(current).strip())
        current.clear()

    for raw in lines[marker_index + 1 :]:
        line = raw.strip()
        if _NOW_STOP_RE.match(line) or _NOW_MACHINE_BLOCK_RE.match(line):
            break
        if not line:
            _flush()
            continue

        loc_match = _LOCATIONS_INCLUDE_RE.match(line)
        if loc_match:
            line = f"{loc_match.group(1)}:"
        current.append(line)

    _flush()
    return "\n".join(p for p in paragraphs if p).strip()

# --- centralized from seasonalweather/broadcast/now.py ---
def build_now_script(product_text: str, *, intro: str) -> str:
    """Build TTS-ready routine-cycle narration for a NOW product."""
    body = extract_now_narrative(product_text)
    if not body:
        return ""

    lead = (intro or "A statement from the National Weather Service.").strip()
    if lead and not lead.endswith((".", "!", "?")):
        lead += "."

    return clean_for_tts(f"{lead}\n{body}").strip()

# --- centralized from seasonalweather/broadcast/pns.py ---
log = logging.getLogger("seasonalweather.broadcast.pns")

# --- centralized from seasonalweather/broadcast/pns.py ---
_NWS_HEADER_ISSUED_RE = re.compile(
    r"^(?P<hhmm>\d{3,4})\s*(?P<ampm>AM|PM)\s*(?P<tz>[A-Z]{2,4})\s+"
    r"(?P<dow>[A-Za-z]{3})\s+(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})\s*$",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/pns.py ---
_UGC_EXPIRY_RE = re.compile(r"-(?P<dd>\d{2})(?P<hh>\d{2})(?P<mm>\d{2})-")

# --- centralized from seasonalweather/broadcast/pns.py ---
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")

# --- centralized from seasonalweather/broadcast/pns.py ---
_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z'-]*\b")

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
def _as_tuple_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(v) for v in value if str(v).strip())
    return ()

# --- centralized from seasonalweather/broadcast/pns.py ---
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
                intro=str(
                    getattr(raw, "intro", "")
                    or "The National Weather Service has issued the following public information statement."
                ),
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
def _headline_lines(text: str) -> list[str]:
    stripped = strip_nws_product_headers(text or "")
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    return lines[:8]

# --- centralized from seasonalweather/broadcast/pns.py ---
def _contains_any(haystack: str, needles: Sequence[str]) -> bool:
    h = haystack.lower()
    return any(str(n).strip().lower() in h for n in needles if str(n).strip())

# --- centralized from seasonalweather/broadcast/pns.py ---
def _contains_all(haystack: str, needles: Sequence[str]) -> bool:
    h = haystack.lower()
    return all(str(n).strip().lower() in h for n in needles if str(n).strip())

# --- centralized from seasonalweather/broadcast/pns.py ---
def _aligned_table_rows(lines: list[str]) -> int:
    count = 0
    for ln in lines:
        s = ln.rstrip()
        if not s:
            continue
        if re.search(r"\S\s{2,}\S", s) and len(_NUMBER_RE.findall(s)) >= 2:
            count += 1
    return count

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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

# --- centralized from seasonalweather/broadcast/pns.py ---
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
            if (
                s.startswith("...")
                or "national weather service" in s.lower()
                or s.lower().startswith("noaa weather radio")
            ):
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

# --- centralized from seasonalweather/broadcast/pns.py ---
def _sha1_12(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "ignore"), usedforsecurity=False).hexdigest()[:12]

# --- centralized from seasonalweather/broadcast/pns.py ---
class PnsStateMachine:
    """Classify a PNS and decide whether it is safe/useful cycle audio."""

    def __init__(self, cfg: Any, *, tz: ZoneInfo) -> None:
        self.policy = policy_from_config(cfg)
        self.tz = tz

    def evaluate(
        self, raw_text: str, *, wfo: str = "", awips_id: str = "", issued: Any = None, now: Any = None
    ) -> PnsDecision:
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
            return PnsDecision(
                action=action,
                signals=signals,
                reason="no_configured_subtype_match",
                issued_utc=issued_utc,
                expires_utc=exp_utc,
            )

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
        if (
            any(s in signals for s in ("table_header", "aligned_rows", "numeric_dense", "latlon_rows"))
            and subtype.name == "severe_weather_safety_rules"
        ):
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
            return PnsDecision(
                action="stale",
                subtype=subtype.name,
                event=subtype.event,
                code=subtype.code,
                reason="missing_issued_time",
                signals=signals,
                expires_utc=exp_utc,
            )
        issued_local = issued_utc.astimezone(self.tz)
        age = now_local - issued_local
        if age.total_seconds() < -300:
            return PnsDecision(
                action="stale",
                subtype=subtype.name,
                event=subtype.event,
                code=subtype.code,
                reason="issued_in_future",
                signals=signals,
                issued_utc=issued_utc,
                expires_utc=exp_utc,
            )
        if subtype.max_fresh_hours > 0 and age > dt.timedelta(hours=subtype.max_fresh_hours):
            return PnsDecision(
                action="stale",
                subtype=subtype.name,
                event=subtype.event,
                code=subtype.code,
                reason="past_freshness_window",
                signals=signals,
                issued_utc=issued_utc,
                expires_utc=exp_utc,
            )
        if subtype.require_same_day and issued_local.date() != now_local.date():
            return PnsDecision(
                action="stale",
                subtype=subtype.name,
                event=subtype.event,
                code=subtype.code,
                reason="not_same_local_day",
                signals=signals,
                issued_utc=issued_utc,
                expires_utc=exp_utc,
            )

        script = _build_script(text, subtype, self.policy.hard_stop_delimiter)
        if not script.strip():
            return PnsDecision(
                action="drop",
                subtype=subtype.name,
                event=subtype.event,
                code=subtype.code,
                reason="no_coherent_spoken_text",
                signals=signals,
                issued_utc=issued_utc,
                expires_utc=exp_utc,
            )

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

# --- centralized from seasonalweather/broadcast/offnt2.py ---
_EXPECTED_AWIPS = "OFFNT2"

# --- centralized from seasonalweather/broadcast/offnt2.py ---
_EXPECTED_WMO = "FZNT22 KWBC"

# --- centralized from seasonalweather/broadcast/offnt2.py ---
_WMO_RE = re.compile(r"\bFZNT\d{2}\s+[A-Z]{4}\b", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
_AWIPS_LINE_RE = re.compile(r"^\s*(OFFNT\d+)\s*$", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
_SYNOPSIS_RE = re.compile(r"^\s*\.?SYNOPSIS(?:\b|\.{3})", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
_SYNOPSIS_PREFIX_RE = re.compile(r"^\s*\.?SYNOPSIS(?:\s+FOR\b[^.]*?WATERS)?\.{0,3}", re.IGNORECASE)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
_PERIOD_RE = re.compile(
    r"^\s*(REST OF TONIGHT|REST OF TODAY|TONIGHT|TODAY|SUN NIGHT|MON NIGHT|TUE NIGHT|WED NIGHT|"
    r"THU NIGHT|FRI NIGHT|SAT NIGHT|SUN|MON|TUE|WED|THU|FRI|SAT)\s*\.{3}(.*)$",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
_PERIOD_MAP = {
    "REST OF TONIGHT": "Rest of tonight",
    "REST OF TODAY": "Rest of today",
    "TONIGHT": "Tonight",
    "TODAY": "Today",
    "SUN": "Sunday",
    "SUN NIGHT": "Sunday night",
    "MON": "Monday",
    "MON NIGHT": "Monday night",
    "TUE": "Tuesday",
    "TUE NIGHT": "Tuesday night",
    "WED": "Wednesday",
    "WED NIGHT": "Wednesday night",
    "THU": "Thursday",
    "THU NIGHT": "Thursday night",
    "FRI": "Friday",
    "FRI NIGHT": "Friday night",
    "SAT": "Saturday",
    "SAT NIGHT": "Saturday night",
}

# --- centralized from seasonalweather/broadcast/offnt2.py ---
_ISSUANCE_RE = re.compile(
    r"^\s*\d{3,4}\s+(?:AM|PM)\s+[A-Z]{3}\s+\w{3}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{4}\s*$",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
_WARNING_RE = re.compile(
    r"\b(?:HURRICANE\s+FORCE\s+WIND|STORM|GALE|TROPICAL\s+STORM|HURRICANE|"
    r"SMALL\s+CRAFT|DENSE\s+FOG)\s+(?:WARNING|WATCH|ADVISORY)\b",
    re.IGNORECASE,
)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
@dataclass(frozen=True)
class Offnt2ZoneForecast:
    """One routed OFFNT2 forecast block and the zones it covers."""

    zone_ids: tuple[str, ...]
    text: str
    warning_headlines: tuple[str, ...] = ()

# --- centralized from seasonalweather/broadcast/offnt2.py ---
@dataclass(frozen=True)
class Offnt2Product:
    """Validated OFFNT2 product content."""

    awips_id: str
    wmo_heading: str
    synopsis: str | None
    zones: tuple[Offnt2ZoneForecast, ...]

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _offnt2_clean_line(value: str) -> str:
    value = value.replace("\r", "").replace("—", "-")
    value = re.sub(r"\s+", " ", value).strip(" \t").lstrip(".").strip()
    return value

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _is_routing_line(value: str) -> bool:
    parts = [part for part in value.strip().split("-") if part]
    if len(parts) < 2 or not re.fullmatch(r"ANZ\d{3}", parts[0], re.IGNORECASE):
        return False
    for part in parts[1:]:
        if re.fullmatch(r"\d{3}", part) or re.fullmatch(r"\d{6}", part):
            continue
        return False
    return bool(re.fullmatch(r"\d{6}", parts[-1]))

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _routing_zones(value: str) -> tuple[str, ...] | None:
    if not _is_routing_line(value):
        return None
    parts = [part for part in value.strip().split("-") if part]
    prefix = parts[0][:3].upper()
    zones = [parts[0].upper()]
    zones.extend(f"{prefix}{part}" for part in parts[1:-1])
    return tuple(zones)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _is_issuance_line(value: str) -> bool:
    return bool(_ISSUANCE_RE.fullmatch(value))

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _spoken_period_line(value: str) -> str:
    match = _PERIOD_RE.match(value)
    if not match:
        return value
    period = _PERIOD_MAP[match.group(1).upper()]
    remainder = match.group(2).strip()
    return f"{period}. {remainder}" if remainder else f"{period}."

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _issuance_preamble_indices(lines: Sequence[str]) -> set[int]:
    skipped: set[int] = set()
    for index, line in enumerate(lines):
        if not _is_issuance_line(_offnt2_clean_line(line)):
            continue
        skipped.add(index)
        for previous in range(index - 1, -1, -1):
            if not _offnt2_clean_line(lines[previous]):
                break
            skipped.add(previous)
    return skipped

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _clean_section(lines: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    cleaned: list[str] = []
    warnings: list[str] = []
    raw_lines = list(lines)
    skipped = _issuance_preamble_indices(raw_lines)
    for index, raw in enumerate(raw_lines):
        line = _offnt2_clean_line(raw)
        if index in skipped or not line or line == "$$" or _is_routing_line(raw):
            continue
        line = _spoken_period_line(line)
        if line.upper() in {
            "OFFSHORE WATERS FORECAST",
            "NATIONAL WEATHER SERVICE",
            "OCEAN PREDICTION CENTER",
        }:
            continue
        match = _WARNING_RE.search(line)
        if match and line.upper() == line and line not in warnings:
            warnings.append(line.strip(" ."))
        cleaned.append(line)
    return " ".join(cleaned), tuple(warnings)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _identity(lines: Sequence[str]) -> tuple[str, str] | None:
    header = "\n".join(lines[:32])
    wmo_values = tuple(match.group(0).upper() for match in _WMO_RE.finditer(header))
    awips_values = tuple(
        match.group(1).upper() for line in lines[:32] if (match := _AWIPS_LINE_RE.match(line)) is not None
    )
    if any(value != _EXPECTED_WMO for value in wmo_values):
        return None
    if any(value != _EXPECTED_AWIPS for value in awips_values):
        return None
    if _EXPECTED_WMO not in wmo_values and _EXPECTED_AWIPS not in awips_values:
        return None
    return _EXPECTED_AWIPS, _EXPECTED_WMO

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _routing_sections(lines: Sequence[str]) -> list[tuple[int, tuple[str, ...]]]:
    routing: list[tuple[int, tuple[str, ...]]] = []
    for index, line in enumerate(lines):
        routing_zones = _routing_zones(line)
        if routing_zones:
            routing.append((index, routing_zones))
    return routing

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _extract_synopsis(lines: Sequence[str]) -> str | None:
    synopsis_lines: list[str] = []
    synopsis_started = False
    for line in lines:
        if _SYNOPSIS_RE.match(line):
            synopsis_started = True
            remainder = _SYNOPSIS_PREFIX_RE.sub("", line).strip(" .")
            if remainder:
                synopsis_lines.append(remainder)
        elif synopsis_started:
            if line.strip() == "$$" or _routing_zones(line):
                break
            synopsis_lines.append(line)
    synopsis, _ = _clean_section(synopsis_lines)
    return synopsis.strip(" .") if synopsis else None

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _parse_zones(
    lines: Sequence[str], routing: Sequence[tuple[int, tuple[str, ...]]]
) -> tuple[Offnt2ZoneForecast, ...]:
    zones: list[Offnt2ZoneForecast] = []
    for position, (start, zone_ids) in enumerate(routing):
        end = routing[position + 1][0] if position + 1 < len(routing) else len(lines)
        section = lines[start + 1 : end]
        if any(_SYNOPSIS_RE.match(line) for line in section):
            continue
        body, warnings = _clean_section(section)
        if body:
            zones.append(Offnt2ZoneForecast(zone_ids=zone_ids, text=body, warning_headlines=warnings))
    return tuple(zones)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def parse_offnt2_product(raw: str | None) -> Offnt2Product | None:
    """Parse and validate one raw product, rejecting unexpected regions."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    lines = raw.replace("\r", "").splitlines()
    identity = _identity(lines)
    if identity is None:
        return None
    routing = _routing_sections(lines)
    if not routing:
        return Offnt2Product(identity[0], identity[1], None, ())
    return Offnt2Product(
        identity[0],
        identity[1],
        _extract_synopsis(lines),
        _parse_zones(lines, routing),
    )

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _canonical(value: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (value or "").lower()).strip()

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _same_synopsis(left: str | None, right: str | None) -> bool:
    a, b = _canonical(left), _canonical(right)
    return bool(a and b and (a == b or a in b or b in a))

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _word_count(value: str) -> int:
    return len(value.split())

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _within_budget(value: str, max_chars: int, max_words: int) -> bool:
    return (not max_chars or len(value) <= max_chars) and (not max_words or _word_count(value) <= max_words)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _append_optional(base: str, addition: str, max_chars: int, max_words: int) -> str:
    candidate = f"{base} {addition}".strip()
    if max_chars and len(candidate) > max_chars:
        return base
    if max_words and _word_count(candidate) > max_words:
        return base
    return candidate

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _zone_parts(label: str, block: Offnt2ZoneForecast) -> tuple[str, str]:
    warning_text = " ".join(block.warning_headlines)
    protected = f"The forecast for {label}."
    if warning_text:
        protected = f"{protected} {warning_text}."
    remaining = block.text
    for warning in block.warning_headlines:
        remaining = re.sub(re.escape(warning), "", remaining, flags=re.IGNORECASE)
    return protected, re.sub(r"\s+", " ", remaining).strip(" .")

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _trim_zone_body(protected: str, remaining: str, max_chars: int, max_words: int) -> str:
    available_words = max_words - _word_count(protected) if max_words else len(remaining.split())
    words: list[str] = []
    for word in remaining.split():
        if len(words) >= available_words:
            break
        trial = " ".join(words + [word])
        if max_chars and len(protected) + 1 + len(trial) > max_chars:
            break
        words.append(word)
    return f"{protected} {' '.join(words)}".strip()

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _append_zone(
    base: str,
    *,
    label: str,
    block: Offnt2ZoneForecast,
    max_chars: int,
    max_words: int,
) -> str | None:
    protected, remaining = _zone_parts(label, block)
    candidate = f"{base} {protected} {remaining}".strip()
    if _within_budget(candidate, max_chars, max_words):
        return candidate

    protected_candidate = f"{base} {protected}".strip()
    if not _within_budget(protected_candidate, max_chars, max_words):
        # A configured budget cannot safely discard a warning headline.  Keep
        # the protected evidence rather than silently dropping the warning.
        return protected_candidate if block.warning_headlines else None
    trimmed = _trim_zone_body(protected_candidate, remaining, max_chars, max_words)
    if trimmed == protected_candidate and not block.warning_headlines:
        return None
    return trimmed

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _configured_zones(configured_zones: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    configured = [(str(zone).upper().strip(), str(label).strip()) for zone, label in configured_zones]
    return [(zone, label or zone) for zone, label in configured if re.fullmatch(r"ANZ\d{3}", zone)]

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _rotated_zones(
    configured: list[tuple[str, str]], rotate_period_s: int, rotate_step: int, now: dt.datetime
) -> list[tuple[str, str]]:
    if not configured:
        return []
    period = max(1, rotate_period_s)
    offset = (int(now.timestamp() // period) * (rotate_step or 1)) % len(configured)
    return configured[offset:] + configured[:offset]

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _selected_zones(product: Offnt2Product, rotated: Sequence[tuple[str, str]]) -> list[tuple[str, Offnt2ZoneForecast]]:
    selected: list[tuple[str, Offnt2ZoneForecast]] = []
    seen_blocks: set[int] = set()
    for zone_id, label in rotated:
        for index, block in enumerate(product.zones):
            if index not in seen_blocks and zone_id in block.zone_ids:
                selected.append((label, block))
                seen_blocks.add(index)
                break
    return selected

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _prioritized_zones(
    selected: list[tuple[str, Offnt2ZoneForecast]], heightened: bool, defer_in_heightened: bool
) -> list[tuple[str, Offnt2ZoneForecast]] | None:
    warning_selected = [(label, block) for label, block in selected if block.warning_headlines]
    if heightened and defer_in_heightened:
        return warning_selected or None
    return warning_selected + [item for item in selected if not item[1].warning_headlines]

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def _append_synopsis(
    text: str,
    product: Offnt2Product,
    *,
    include_synopsis: bool,
    heightened: bool,
    defer_in_heightened: bool,
    cwf_synopsis: str | None,
    max_chars: int,
    max_words: int,
) -> str:
    if not include_synopsis or not product.synopsis or (heightened and defer_in_heightened):
        return text
    if _same_synopsis(product.synopsis, cwf_synopsis):
        return text
    return _append_optional(text, f"Synopsis. {product.synopsis}.", max_chars, max_words)

# --- centralized from seasonalweather/broadcast/offnt2.py ---
def render_offnt2(
    product: Offnt2Product,
    *,
    configured_zones: Sequence[tuple[str, str]],
    include_synopsis: bool,
    rotate_period_s: int,
    rotate_step: int,
    now: dt.datetime,
    max_chars: int = 0,
    max_airtime_seconds: int = 0,
    heightened: bool = False,
    defer_in_heightened: bool = True,
    cwf_synopsis: str | None = None,
) -> str | None:
    """Render configured zones with deterministic rotation and hard budgets."""
    configured = _configured_zones(configured_zones)
    selected = _selected_zones(product, _rotated_zones(configured, rotate_period_s, rotate_step, now))
    if not selected:
        return None
    prioritized = _prioritized_zones(selected, heightened, defer_in_heightened)
    if prioritized is None:
        return None
    selected = prioritized

    max_words = floor(max_airtime_seconds * 2.5) if max_airtime_seconds > 0 else 0
    text = "And now for the offshore waters forecast for our area."
    text = _append_synopsis(
        text,
        product,
        include_synopsis=include_synopsis,
        heightened=heightened,
        defer_in_heightened=defer_in_heightened,
        cwf_synopsis=cwf_synopsis,
        max_chars=max_chars,
        max_words=max_words,
    )
    for label, block in selected:
        appended = _append_zone(text, label=label, block=block, max_chars=max_chars, max_words=max_words)
        if appended is None:
            break
        text = appended
    return text

# --- centralized from seasonalweather/broadcast/offnt2.py ---
__all__ = [
    "Offnt2Product",
    "Offnt2ZoneForecast",
    "parse_offnt2_product",
    "render_offnt2",
]

# --- centralized from seasonalweather/broadcast/rwr.py ---
_SKY_SPOKEN: Dict[str, str] = {
    # ------------------------------------------------------------------
    # Sky condition only
    # ------------------------------------------------------------------
    "CLOUDY":   "cloudy",
    "MOCLDY":   "mostly cloudy",
    "PTCLDY":   "partly cloudy",
    "FAIR":     "fair",
    "CLEAR":    "clear",
    "SUNNY":    "sunny",
    "MOSUNNY":  "mostly sunny",
    "PTSUNNY":  "partly sunny",
    "OVERCAST": "overcast",

    # ------------------------------------------------------------------
    # Obscurations / visibility phenomena
    # ------------------------------------------------------------------
    "FOG":          "fog",
    "FZFOG":        "freezing fog",
    "FOG/MIST":     "fog",                    # api.weather.gov composite
    "FREEZINGFOG":  "freezing fog",           # ASOS space-stripped form
    "MIST":         "mist",
    "HAZE":         "haze",
    "SMOKE":        "smoke",
    "DUST":         "dust",
    "BLDU":         "blowing dust",
    "BLDS":         "blowing dust",
    "BLGSNO":       "blowing snow",
    "BLGSNO+":      "heavy blowing snow",

    # ------------------------------------------------------------------
    # Rain
    # RWR spaced form | concatenated ASOS-path form | api.weather.gov form
    # ------------------------------------------------------------------
    "RAIN":             "rain",
    "LGT RAIN":         "light rain",
    "HVY RAIN":         "heavy rain",
    "LGTRAIN":          "light rain",          # space-stripped RWR
    "HVYRAIN":          "heavy rain",          # space-stripped RWR
    "LIGHTRAIN":        "light rain",          # api.weather.gov
    "HEAVYRAIN":        "heavy rain",          # api.weather.gov
    "RAIN SHWRS":       "rain showers",
    "RAINSHWRS":        "rain showers",
    "RAINSHOWERS":      "rain showers",
    "LGT RAIN SHWRS":   "light rain showers",
    "HVY RAIN SHWRS":   "heavy rain showers",
    "LIGHTRAINSHOWERS": "light rain showers",  # api.weather.gov
    "HEAVYRAINSHOWERS": "heavy rain showers",  # api.weather.gov

    # ------------------------------------------------------------------
    # Drizzle
    # ------------------------------------------------------------------
    "DRZL":                 "drizzle",
    "DRIZZLE":              "drizzle",
    "LGT DRZL":             "light drizzle",
    "HVY DRZL":             "heavy drizzle",
    "LGTDRZL":              "light drizzle",
    "HVYDRZL":              "heavy drizzle",
    "LIGHTDRIZZLE":         "light drizzle",
    "HEAVYDRIZZLE":         "heavy drizzle",

    # ------------------------------------------------------------------
    # Freezing rain
    # ------------------------------------------------------------------
    "FZRAIN":               "freezing rain",
    "LGT FZRN":             "light freezing rain",
    "HVY FZRN":             "heavy freezing rain",
    "LGTFZRN":              "light freezing rain",
    "HVYFZRN":              "heavy freezing rain",
    "LIGHTFREEZINGRAIN":    "light freezing rain",
    "HEAVYFREEZINGRAIN":    "heavy freezing rain",

    # ------------------------------------------------------------------
    # Freezing drizzle
    # ------------------------------------------------------------------
    "FZDRZL":               "freezing drizzle",
    "LGT FZDRZL":           "light freezing drizzle",
    "HVY FZDRZL":           "heavy freezing drizzle",
    "LGTFZDRZL":            "light freezing drizzle",
    "HVYFZDRZL":            "heavy freezing drizzle",
    "LIGHTFREEZINGDRIZZLE": "light freezing drizzle",
    "HEAVYFREEZINGDRIZZLE": "heavy freezing drizzle",

    # ------------------------------------------------------------------
    # Snow
    # ------------------------------------------------------------------
    "SNOW":             "snow",
    "LGT SNOW":         "light snow",
    "HVY SNOW":         "heavy snow",
    "LGTSNOW":          "light snow",
    "HVYSNOW":          "heavy snow",
    "LIGHTSNOW":        "light snow",
    "HEAVYSNOW":        "heavy snow",
    "FLRIES":           "snow flurries",
    "FLURRIES":         "snow flurries",
    "SNOW SHWRS":       "snow showers",
    "SNOWSHWRS":        "snow showers",
    "SNOWSHOWERS":      "snow showers",
    "LGT SNOW SHWRS":   "light snow showers",
    "HVY SNOW SHWRS":   "heavy snow showers",
    "LIGHTSNOWSHOWERS": "light snow showers",  # api.weather.gov
    "LIGHTSNOWSHOWER":  "light snow showers",  # api.weather.gov variant
    "HEAVYSNOWSHOWERS": "heavy snow showers",
    "BLIZZD":           "blizzard conditions",
    "BLGSNO":           "blowing snow",        # duplicate handled; last wins

    # ------------------------------------------------------------------
    # Sleet / ice pellets
    # ------------------------------------------------------------------
    "SLEET":            "sleet",
    "LGT SLEET":        "light sleet",
    "HVY SLEET":        "heavy sleet",
    "LGTSLEET":         "light sleet",
    "HVYSLEET":         "heavy sleet",
    "LIGHTSLEET":       "light sleet",
    "HEAVYSLEET":       "heavy sleet",
    "ICEGRPL":          "ice and snow pellets",
    "ICEPELLETS":       "ice pellets",
    "LIGHTICEANDSNOWPELLETS": "light ice and snow pellets",
    "LIGHTICE":         "light ice pellets",
    "HEAVYICE":         "heavy ice pellets",

    # ------------------------------------------------------------------
    # Mixed precipitation
    # ------------------------------------------------------------------
    "RAINANDSNOW":      "rain and snow",
    "SNOWANDRAIN":      "snow and rain",
    "WINTRY MIX":       "wintry mix",
    "WINTRYMIX":        "wintry mix",

    # ------------------------------------------------------------------
    # Thunderstorms
    # ------------------------------------------------------------------
    "TSTRM":            "thunderstorms",
    "TSTMS":            "thunderstorms",
    "TSRAIN":           "thunderstorms and rain",
    "THUNDERSTORM":     "thunderstorm",
    "THUNDERSTORMS":    "thunderstorms",
    "LIGHTTHUNDERSTORMANDHEAVYRAIN": "thunderstorm with heavy rain",
    "LIGHTTHUNDERSTORMANDRAIN":      "thunderstorm with rain",
    "HEAVYTHUNDERSTORMANDRAIN":      "severe thunderstorm with rain",
    "THUNDERSTORMWITHRAIN":          "thunderstorm with rain",
    "THUNDERSTORMWITHHEAVYRAIN":     "thunderstorm with heavy rain",
    "THUNDERSTORMWITHLIGHTRAIN":     "thunderstorm with light rain",
}

# --- centralized from seasonalweather/broadcast/rwr.py ---
_COMPASS_DIRS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]

# --- centralized from seasonalweather/broadcast/rwr.py ---
_COMPASS_SPOKEN: Dict[str, str] = {
    "N":   "north",          "NNE": "north-northeast",
    "NE":  "northeast",      "ENE": "east-northeast",
    "E":   "east",           "ESE": "east-southeast",
    "SE":  "southeast",      "SSE": "south-southeast",
    "S":   "south",          "SSW": "south-southwest",
    "SW":  "southwest",      "WSW": "west-southwest",
    "W":   "west",           "WNW": "west-northwest",
    "NW":  "northwest",      "NNW": "north-northwest",
}

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _degrees_to_compass(degrees: float) -> str:
    """Convert wind direction in degrees to 16-point compass abbreviation."""
    idx = round(float(degrees) / 22.5) % 16
    return _COMPASS_DIRS[idx]

# --- centralized from seasonalweather/broadcast/rwr.py ---
_TREND_SPOKEN: Dict[str, str] = {
    "R": "rising",
    "F": "falling",
    "S": "steady",
}

# --- centralized from seasonalweather/broadcast/rwr.py ---
_SECTION_SPOKEN: Dict[str, Optional[str]] = {
    "WASHINGTON METRO":                "the Washington metro area",
    "BALTIMORE METRO":                 "the Baltimore metro area",
    "MARYLAND EASTERN SHORE":          "the Maryland Eastern Shore",
    "SOUTHERN MARYLAND":               "southern Maryland",
    "NORTH CENTRAL MARYLAND":          "north central Maryland",
    "WESTERN MARYLAND":                "western Maryland",
    "SHENANDOAH VALLEY":               "the Shenandoah Valley",
    "EASTERN WEST VIRGINIA PANHANDLE": "eastern West Virginia",
    "CENTRAL FOOTHILLS":               "the central foothills",
    "NORTH AND CENTRAL PIEDMONT":      "the north and central Piedmont",
    "OTHER REGIONAL OBSERVATIONS":     None,
}

# --- centralized from seasonalweather/broadcast/rwr.py ---
_DEFAULT_STATION_NAMES: Dict[str, str] = {
    "WASHINGTON NAT":   "Reagan National Airport",
    "DULLES":           "Dulles International Airport",
    "ANDREWS AFB":      "Joint Base Andrews",
    "FT BELVOIR":       "Fort Belvoir",
    "QUANTICO":         "Quantico",
    "COLLEGE PARK":     "College Park Airport",
    "LEESBURG":         "Leesburg",
    "MANASSAS":         "Manassas Airport",
    "GAITHERSBURG":     "Gaithersburg",
    "BWI AIRPORT":      "Baltimore-Washington International Airport",
    "BALT INNER HAR":   "Baltimore Inner Harbor",
    "MARTIN STATE":     "Martin State Airport",
    "ANNAPOLIS":        "Annapolis",
    "FORT MEADE":       "Fort Meade",
    "OCEAN CITY":       "Ocean City",
    "SALISBURY":        "Salisbury",
    "CAMBRIDGE":        "Cambridge",
    "EASTON":           "Easton",
    "PATUXENT RIVER":   "Patuxent River Naval Air Station",
    "ST INIGOES":       "Saint Inigoes",
    "FREDERICK":        "Frederick",
    "HAGERSTOWN APT":   "Hagerstown Regional Airport",
    "WESTMINSTER":      "Westminster",
    "OAKLAND":          "Oakland",
    "CUMBERLAND":       "Cumberland",
    "WINCHESTER":       "Winchester",
    "NEW MARKET":       "New Market",
    "STAUNTON":         "Staunton",
    "WAYNESBORO":       "Waynesboro",
    "MARTINSBURG":      "Martinsburg",
    "PETERSBURG":       "Petersburg West Virginia",
    "CHARLOTTESVILLE":  "Charlottesville",
    "CULPEPER":         "Culpeper",
    "ORANGE":           "Orange",
    "GORDONSVILLE":     "Gordonsville",
    "WARRENTON":        "Warrenton",
    "FREDERICKSBURG":   "Fredericksburg",
    "FREDERICKSBG":     "Fredericksburg",
    "NEW YORK CITY":    "New York City",
    "PHILADELPHIA":     "Philadelphia",
    "PITTSBURGH":       "Pittsburgh",
    "ROANOKE":          "Roanoke",
    "RICHMOND":         "Richmond",
    "RALEIGH":          "Raleigh",
    "CHARLOTTSVILL":    "Charlottesville",
}

# --- centralized from seasonalweather/broadcast/rwr.py ---
@dataclass
class RwrStation:
    name: str                         # cleaned, spoken-ready name
    name_raw: str                     # original from product (for anchor matching)
    sky_raw: str                      # raw sky code (e.g. "MOCLDY")
    temp_f: Optional[int]
    dewpoint_f: Optional[int]
    rh: Optional[int]
    wind_raw: str                     # raw wind field (e.g. "S7", "NW15G26", "CALM")
    pres_raw: str                     # raw pressure+trend (e.g. "29.95R")
    remarks: str
    is_nws: bool = True

# --- centralized from seasonalweather/broadcast/rwr.py ---
@dataclass
class RwrSection:
    title: str                        # e.g. "WASHINGTON METRO"
    zone_codes: List[str]             # parsed from routing line
    stations: List[RwrStation]
    is_marine: bool = False

# --- centralized from seasonalweather/broadcast/rwr.py ---
@dataclass
class RwrMarineStation:
    """One station row from the RWR MARINE OBSERVATIONS section."""
    name: str               # spoken-ready name (from name_map or title-cased raw)
    name_raw: str           # upper-case original from product, e.g. "THOMAS PT LIGHT"
    obs_time_utc: Optional[str]   # HHMM UTC string, e.g. "1800"
    air_temp_f: Optional[int]
    sea_temp_f: Optional[int]
    wind_dir_deg: Optional[int]   # degrees true (0-359)
    wind_spd_kt: Optional[int]
    wind_gust_kt: Optional[int]
    pres_mb: Optional[float]
    pres_trend: Optional[str]

# --- centralized from seasonalweather/broadcast/rwr.py ---
@dataclass
class RwrProduct:
    issuance_time_str: Optional[str]  # e.g. "1:00 AM Eastern Daylight Time"
    issuance_dt: Optional[dt.datetime]
    office: str
    sections: List[RwrSection]
    marine_stations: List[RwrMarineStation] = field(default_factory=list)

# --- centralized from seasonalweather/broadcast/rwr.py ---
_RWR_TIME_RE = re.compile(
    r'\b(\d{3,4})\s+(AM|PM)\s+([A-Z]{2,4})\s+'
    r'(?:MON|TUE|WED|THU|FRI|SAT|SUN)\s+'
    r'([A-Z]{3})\s+(\d{1,2})\s+(\d{4})'
)

# --- centralized from seasonalweather/broadcast/rwr.py ---
_TZ_SPOKEN: Dict[str, str] = {
    "EST": "Eastern Standard Time",
    "EDT": "Eastern Daylight Time",
    "CST": "Central Standard Time",
    "CDT": "Central Daylight Time",
    "MST": "Mountain Standard Time",
    "MDT": "Mountain Daylight Time",
    "PST": "Pacific Standard Time",
    "PDT": "Pacific Daylight Time",
}

# --- centralized from seasonalweather/broadcast/rwr.py ---
_MONTH_MAP: Dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _parse_rwr_time(text: str) -> Tuple[Optional[str], Optional[dt.datetime]]:
    """
    Parse the issuance time line from RWR product text.
    Returns (spoken_time_str, datetime_utc_approx).
    e.g. "100 AM EDT SUN MAR 22 2026" -> ("1:00 AM Eastern Daylight Time", datetime(...))
    """
    m = _RWR_TIME_RE.search(text)
    if not m:
        return None, None
    hm_raw, ampm, tz_abbr, mon_str, day_str, year_str = (
        m.group(1), m.group(2), m.group(3),
        m.group(4), m.group(5), m.group(6),
    )
    # Parse hour/minute
    if len(hm_raw) == 3:
        h = int(hm_raw[0])
        mins = hm_raw[1:]
    else:
        h = int(hm_raw[:2])
        mins = hm_raw[2:]
    spoken = f"{h}:{mins} {ampm} {_TZ_SPOKEN.get(tz_abbr, tz_abbr)}"

    # Best-effort UTC datetime (for staleness check)
    try:
        month = _MONTH_MAP.get(mon_str.upper(), 1)
        day = int(day_str)
        year = int(year_str)
        h24 = h if ampm == "AM" else (h + 12 if h < 12 else 12)
        if ampm == "AM" and h == 12:
            h24 = 0
        # Approximate UTC (EDT = UTC-4, EST = UTC-5)
        tz_offset_h = {"EDT": -4, "EST": -5, "CDT": -5, "CST": -6,
                       "MDT": -6, "MST": -7, "PDT": -7, "PST": -8}.get(tz_abbr, 0)
        naive = dt.datetime(year, month, day, h24, int(mins))
        utc_approx = naive - dt.timedelta(hours=tz_offset_h)
        aware = utc_approx.replace(tzinfo=dt.timezone.utc)
        return spoken, aware
    except Exception:
        return spoken, None

# --- centralized from seasonalweather/broadcast/rwr.py ---
_RWR_ROUTING_RE = re.compile(r'^[A-Z]{2}[Z0-9]\d{2,3}[>A-Z0-9-]+-\d{6}-\s*$')

# --- centralized from seasonalweather/broadcast/rwr.py ---
_RWR_CITY_HEADER_RE = re.compile(r'^CITY\s+SKY/WX')

# --- centralized from seasonalweather/broadcast/rwr.py ---
_RWR_DATA_SKIP_RE = re.compile(
    r'^(?:\$\$|={3,}|Note:|TC=|\*\s*=|STATION/POSITION|AIR SEA)'
)

# --- centralized from seasonalweather/broadcast/rwr.py ---
_MARINE_WIND_RE = re.compile(r'\b(\d{3})/\s*(\d{1,3})/\s*(\d{1,3})\b')

# --- centralized from seasonalweather/broadcast/rwr.py ---
_MARINE_PRES_RE = re.compile(r'(\d{4}\.\d)([RFS]?)')

# --- centralized from seasonalweather/broadcast/rwr.py ---
_MARINE_TIME_RE = re.compile(r'\b(\d{4})\b')

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _find_cols(header: str) -> Dict[str, int]:
    """
    Find column start positions from the CITY/SKY header line.
    Handles any WFO's column layout by searching for column names.
    """
    cols: Dict[str, int] = {"CITY": 0}
    for name in ("SKY/WX", "TMP", "DP", "RH", "WIND", "PRES", "REMARKS"):
        idx = header.find(name)
        if idx >= 0:
            cols[name] = idx
    return cols

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _slice_col(row: str, cols: Dict[str, int], key: str, next_key: str) -> str:
    start = cols.get(key, -1)
    if start < 0 or start >= len(row):
        return ""
    # End is start of next column (or end of string if next not found)
    end = cols.get(next_key)
    if end is None:
        end = len(row)
    return row[start:end].strip()

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _parse_rwr_wind(s: str) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """
    Returns (direction_abbr, speed_mph, gust_mph).
    direction_abbr: compass abbr like 'S', 'NW', 'VRB', or None for calm/missing.
    """
    s = (s or "").strip().upper()
    if not s or s in ("MISG", "N/A", "M", ""):
        return None, None, None
    if s == "CALM":
        return None, 0, None
    m = re.match(r'^VRB(\d+)$', s)
    if m:
        return "VRB", int(m.group(1)), None
    m = re.match(r'^([NSEW]{1,3})(\d+)(?:G(\d+))?$', s)
    if m:
        gust = int(m.group(3)) if m.group(3) else None
        return m.group(1), int(m.group(2)), gust
    return None, None, None

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _parse_rwr_pressure(s: str) -> Tuple[Optional[float], Optional[str]]:
    """Returns (pressure_inhg, trend_char 'R'/'F'/'S'/None)."""
    s = (s or "").strip().upper()
    m = re.match(r'^(\d{2}\.\d{2})([RFS]?)', s)
    if m:
        return float(m.group(1)), (m.group(2) or None)
    return None, None

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _parse_int(s: str) -> Optional[int]:
    s = (s or "").strip()
    if s in ("N/A", "M", "MISG", "NOT AVBL", ""):
        return None
    try:
        return int(s)
    except ValueError:
        return None

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _clean_station_name(raw: str, name_map: Dict[str, str]) -> Tuple[str, str, bool]:
    """
    Returns (spoken_name, raw_name_upper, is_nws).
    NWS RWR marks non-NWS stations with a trailing * in the name field
    (e.g. "COLLEGE PARK*", "LEESBURG*"). Strip it and record is_nws=False.
    """
    s = raw.strip()
    is_nws = "*" not in s
    s = s.replace("*", "").strip()
    raw_upper = s.upper()
    spoken = name_map.get(raw_upper) or _DEFAULT_STATION_NAMES.get(raw_upper)
    if not spoken:
        # Title-case the abbreviation as-is (e.g. "FORT MEADE" -> "Fort Meade")
        spoken = raw_upper.title()
    return spoken, raw_upper, is_nws

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _parse_data_row(
    row: str,
    cols: Dict[str, int],
    name_map: Dict[str, str],
) -> Optional[RwrStation]:
    """Parse a single RWR data row using column positions from the header."""
    if not row.strip() or _RWR_DATA_SKIP_RE.match(row.strip()):
        return None
    # Need at least the station name region
    if len(row) < cols.get("TMP", 25):
        return None

    raw_name = _slice_col(row, cols, "CITY", "SKY/WX")
    if not raw_name or raw_name.upper() in ("CITY",):
        return None

    # Skip column-header lines
    if raw_name.upper().startswith("CITY"):
        return None

    sky = _slice_col(row, cols, "SKY/WX", "TMP")
    temp_str = _slice_col(row, cols, "TMP", "DP")
    dp_str = _slice_col(row, cols, "DP", "RH")
    rh_str = _slice_col(row, cols, "RH", "WIND")

    # Wind and pressure: NWS right-justifies the pressure value, so it can start
    # 1 char before the "PRES" header label position. Use a regex on the tail
    # of the row (from WIND column onward) to find the pressure value robustly.
    wind_start = cols.get("WIND", 35)
    tail = row[wind_start:].rstrip()

    pres_raw = ""
    remarks = ""
    wind_end_in_tail = len(tail)

    pres_m = re.search(r'(\d{2}\.\d{2}[RFS]?)', tail)
    if pres_m:
        pres_raw = pres_m.group(0)
        wind_end_in_tail = pres_m.start()
        remarks = tail[pres_m.end():].strip()

    wind_raw = tail[:wind_end_in_tail].strip()

    # Skip if name looks like a section header (no numeric data present)
    if re.match(r'^[A-Z\s]{15,}$', raw_name) and not pres_raw and not temp_str:
        return None

    spoken_name, raw_upper, is_nws = _clean_station_name(raw_name, name_map)
    temp_f = _parse_int(temp_str)
    dp_f = _parse_int(dp_str)
    rh = _parse_int(rh_str)

    return RwrStation(
        name=spoken_name,
        name_raw=raw_upper,
        sky_raw=(sky.upper() if sky else ""),
        temp_f=temp_f,
        dewpoint_f=dp_f,
        rh=rh,
        wind_raw=wind_raw,
        pres_raw=pres_raw,
        remarks=remarks,
        is_nws=is_nws,
    )

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _parse_marine_data_row(
    line: str,
    name_map: Dict[str, str],
) -> Optional[RwrMarineStation]:
    """
    Parse one RWR MARINE OBSERVATIONS data row.

    Format (station name fixed 16-char field, then regex-anchored fields):
      THOMAS PT LIGHT  1800   75     150/ 13/ 13 1020.7F
      TOLCHESTER       1730   73 55  220/  9/ 10 1019.8F
      PINEY POINT      1730          130/  7/  8   N/A

    Wind "DDD/ SS/ GG" is the reliable column anchor; everything before it
    contains the time and optional air/sea temp values.
    """
    line = (line or "").rstrip()
    if len(line) < 20:
        return None

    stripped = line.strip().upper()
    if not stripped or stripped == "$$":
        return None
    # Skip the 3-line inner header block
    if (stripped.startswith("STATION") or stripped.startswith("AIR ")
            or stripped.startswith("(UTC")):
        return None

    # Wind field is the reliable anchor: "DDD/ SS/ GG"
    wind_m = _MARINE_WIND_RE.search(line)
    if not wind_m:
        return None

    wind_dir_deg = int(wind_m.group(1))
    wind_spd_kt  = int(wind_m.group(2))
    wind_gust_kt = int(wind_m.group(3))

    # Station name: fixed 16-char field at start of line
    name_raw = line[:16].strip().upper()
    if not name_raw:
        return None

    # Time: first 4-digit HHMM in cols 16-26
    time_region = line[16:26] if len(line) > 16 else ""
    time_m = _MARINE_TIME_RE.search(time_region)
    obs_time_utc = time_m.group(1) if time_m else None

    # Temperatures: any 2-3 digit integers between end-of-time and start-of-wind
    time_end_in_line = (16 + time_m.end()) if time_m else 21
    temp_region = line[time_end_in_line:wind_m.start()]
    temp_nums = re.findall(r'\d{2,3}', temp_region)
    air_temp_f: Optional[int] = int(temp_nums[0]) if len(temp_nums) >= 1 else None
    sea_temp_f: Optional[int] = int(temp_nums[1]) if len(temp_nums) >= 2 else None

    # Pressure: after wind field; N/A is silently absent
    after_wind = line[wind_m.end():]
    pres_m = _MARINE_PRES_RE.search(after_wind)
    pres_mb:    Optional[float] = float(pres_m.group(1)) if pres_m else None
    pres_trend: Optional[str]  = (pres_m.group(2) or None) if pres_m else None

    spoken = name_map.get(name_raw) or name_raw.title()

    return RwrMarineStation(
        name=spoken,
        name_raw=name_raw,
        obs_time_utc=obs_time_utc,
        air_temp_f=air_temp_f,
        sea_temp_f=sea_temp_f,
        wind_dir_deg=wind_dir_deg,
        wind_spd_kt=wind_spd_kt,
        wind_gust_kt=wind_gust_kt,
        pres_mb=pres_mb,
        pres_trend=pres_trend,
    )

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _parse_marine_section(
    lines: List[str],
    i: int,
    n: int,
    name_map: Dict[str, str],
) -> Tuple[List[RwrMarineStation], int]:
    """
    Consume a marine-obs section from lines[i:] up to and including $$.
    Returns (parsed_stations, new_i).
    """
    stations: List[RwrMarineStation] = []
    while i < n:
        ln = lines[i]
        if ln.strip() == "$$":
            i += 1
            break
        st = _parse_marine_data_row(ln, name_map)
        if st:
            stations.append(st)
        i += 1
    return stations, i

# --- centralized from seasonalweather/broadcast/rwr.py ---
def parse_rwr(text: str, name_map: Optional[Dict[str, str]] = None) -> Optional[RwrProduct]:
    """
    Parse a raw NWS RWR product text string into a structured RwrProduct.
    Returns None if the text doesn't look like an RWR product.
    name_map: optional {RAW_UPPER: spoken_name} overrides for station names.
    """
    nm = {k.upper(): v for k, v in (name_map or {}).items()}
    lines = (text or "").replace("\r", "").splitlines()

    # Extract issuance time from product header
    issuance_spoken, issuance_dt = _parse_rwr_time(text)

    # Find issuing office (line after WMO header: "RWRLWX" -> "LWX")
    office = ""
    for ln in lines[:10]:
        m = re.match(r'^RWR([A-Z]{2,4})\s*$', ln.strip())
        if m:
            office = m.group(1)
            break

    sections: List[RwrSection] = []
    all_marine: List[RwrMarineStation] = []
    i = 0
    n = len(lines)

    while i < n:
        ln = lines[i].rstrip()

        # Detect section routing line
        if _RWR_ROUTING_RE.match(ln.strip()):
            zone_codes = re.findall(r'[A-Z]{2}[Z0-9]\d{2,3}', ln)
            i += 1

            # Next non-empty line = section title
            section_title = ""
            while i < n and not lines[i].strip():
                i += 1
            if i < n:
                section_title = lines[i].strip()
                i += 1

            # Is this a marine observations section?
            is_marine = bool(re.search(r'MARINE|BUOY|OFFSHORE', section_title, re.IGNORECASE))

            if is_marine:
                # Marine sections use a completely different fixed-width format.
                # Hand off to the dedicated parser (phase 2).
                parsed, i = _parse_marine_section(lines, i, n, nm)
                all_marine.extend(parsed)
                continue

            # Find the CITY/SKY header line (land sections only)
            cols: Dict[str, int] = {}
            while i < n:
                ln2 = lines[i]
                if _RWR_CITY_HEADER_RE.match(ln2):
                    cols = _find_cols(ln2)
                    i += 1
                    break
                if ln2.strip() == "$$":
                    break
                i += 1

            if not cols:
                # No header found, skip section
                while i < n and lines[i].strip() != "$$":
                    i += 1
                i += 1  # skip $$
                continue

            # Parse data rows until $$
            stations: List[RwrStation] = []
            while i < n:
                ln2 = lines[i]
                if ln2.strip() == "$$":
                    i += 1
                    break
                station = _parse_data_row(ln2, cols, nm)
                if station:
                    stations.append(station)
                i += 1

            if stations or is_marine:
                sections.append(RwrSection(
                    title=section_title,
                    zone_codes=zone_codes,
                    stations=stations,
                    is_marine=is_marine,
                ))
            continue

        i += 1

    if not sections and not all_marine:
        return None

    return RwrProduct(
        issuance_time_str=issuance_spoken,
        issuance_dt=issuance_dt,
        office=office,
        sections=sections,
        marine_stations=all_marine,
    )

# --- centralized from seasonalweather/broadcast/rwr.py ---
class ObsPressureCache:
    """
    Persistent per-station pressure history for trend derivation.
    Survives service restarts (JSON file in work_dir).
    """

    def __init__(
        self,
        path: str,
        max_hours: float = 3.0,
        trend_threshold_inhg: float = 0.02,
    ) -> None:
        self._path = Path(path)
        self._max_secs = max_hours * 3600
        self._threshold = trend_threshold_inhg
        self._data: Dict[str, List[Dict]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            pass

    def _now_iso(self) -> str:
        return dt.datetime.now(tz=dt.timezone.utc).isoformat()

    def _prune(self, station_id: str) -> None:
        cutoff = (
            dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=self._max_secs)
        ).isoformat()
        if station_id in self._data:
            self._data[station_id] = [
                e for e in self._data[station_id] if e.get("ts", "") >= cutoff
            ]

    def update(self, station_id: str, pressure_inhg: float) -> None:
        """Record a new pressure reading for the given station."""
        sid = station_id.strip().upper()
        if sid not in self._data:
            self._data[sid] = []
        self._data[sid].append({"ts": self._now_iso(), "p": round(pressure_inhg, 4)})
        self._prune(sid)
        self._save()

    def get_trend(self, station_id: str, current_inhg: float) -> Optional[str]:
        """
        Derive 'rising', 'falling', or 'steady' by comparing current pressure
        to the oldest cached reading within the window.
        Returns None if insufficient history (< 2 readings).
        """
        sid = station_id.strip().upper()
        self._prune(sid)
        entries = self._data.get(sid, [])
        if len(entries) < 2:
            return None
        # Compare to the oldest in-window reading
        delta = current_inhg - entries[0]["p"]
        if delta > self._threshold:
            return "rising"
        if delta < -self._threshold:
            return "falling"
        return "steady"

# --- centralized from seasonalweather/broadcast/rwr.py ---
def asos_to_rwr_station(
    station_id: str,
    props: Dict[str, Any],
    name_map: Optional[Dict[str, str]] = None,
    station_name_override: Optional[str] = None,
    cache: Optional[ObsPressureCache] = None,
) -> Optional[RwrStation]:
    """
    Convert an api.weather.gov observations/latest response dict to RwrStation.

    Temperature, dew point are in Celsius (converted to F).
    Wind speed is in m/s (converted to mph).
    Pressure is in Pascals (converted to inHg).
    """
    if not props:
        return None

    sid = station_id.strip().upper()

    # Determine spoken station name
    nm = {k.upper(): v for k, v in (name_map or {}).items()}
    if station_name_override:
        spoken_name = station_name_override
    else:
        spoken_name = nm.get(sid) or _DEFAULT_STATION_NAMES.get(sid, sid.title())

    # Sky / text description
    sky_text = (props.get("textDescription") or "").strip().lower()

    # Temperature (Celsius -> F)
    temp_f: Optional[int] = None
    temp_c = (props.get("temperature") or {}).get("value")
    if isinstance(temp_c, (int, float)):
        temp_f = round(temp_c * 9 / 5 + 32)

    # Dew point (Celsius -> F)
    dp_f: Optional[int] = None
    dp_c = (props.get("dewpoint") or {}).get("value")
    if isinstance(dp_c, (int, float)):
        dp_f = round(dp_c * 9 / 5 + 32)

    # Relative humidity
    rh: Optional[int] = None
    rh_v = (props.get("relativeHumidity") or {}).get("value")
    if isinstance(rh_v, (int, float)):
        rh = round(rh_v)

    # Wind direction (degrees -> compass abbreviation)
    wind_raw = ""
    wind_dir_deg = (props.get("windDirection") or {}).get("value")
    wind_speed_mps = (props.get("windSpeed") or {}).get("value")
    wind_gust_mps = (props.get("windGust") or {}).get("value")

    if isinstance(wind_speed_mps, (int, float)):
        speed_mph = round(wind_speed_mps * 2.23694)
        if speed_mph == 0:
            wind_raw = "CALM"
        elif isinstance(wind_dir_deg, (int, float)):
            compass = _degrees_to_compass(wind_dir_deg)
            wind_raw = f"{compass}{speed_mph}"
            if isinstance(wind_gust_mps, (int, float)):
                gust_mph = round(wind_gust_mps * 2.23694)
                if gust_mph > speed_mph:
                    wind_raw += f"G{gust_mph}"
        else:
            wind_raw = f"VRB{speed_mph}"

    # Pressure (Pascals -> inHg)
    pres_raw = ""
    pres_pa = (props.get("seaLevelPressure") or {}).get("value")
    if isinstance(pres_pa, (int, float)):
        pres_inhg = pres_pa / 3386.39
        # Derive trend from cache if available
        trend_char = ""
        if cache is not None:
            trend = cache.get_trend(sid, pres_inhg)
            trend_char = {"rising": "R", "falling": "F", "steady": "S"}.get(trend or "", "")
            cache.update(sid, pres_inhg)
        pres_raw = f"{pres_inhg:.2f}{trend_char}"

    return RwrStation(
        name=spoken_name,
        name_raw=sid,
        sky_raw=sky_text.upper().replace(" ", ""),  # store normalised for lookup
        temp_f=temp_f,
        dewpoint_f=dp_f,
        rh=rh,
        wind_raw=wind_raw,
        pres_raw=pres_raw,
        remarks="",
        is_nws=True,
    )

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _sky_spoken(sky_raw: str, sky_text_fallback: str = "") -> str:
    """
    Return spoken sky condition string.
    sky_raw: RWR sky code (e.g. 'MOCLDY') or normalised ASOS code.
    sky_text_fallback: ASOS textDescription lower-case (used if code lookup fails).
    """
    code = (sky_raw or "").strip().upper()
    spoken = _SKY_SPOKEN.get(code)
    if spoken:
        return spoken
    # Try the ASOS text description directly (already English)
    fb = (sky_text_fallback or "").strip().lower()
    if fb and fb not in ("n/a", "not available", ""):
        return fb
    # Last resort: title-case the raw code
    if code and code not in ("N/A", ""):
        return code.title()
    return ""

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _format_wind_spoken(wind_raw: str) -> Optional[str]:
    """
    Returns spoken wind phrase like 'Winds were south at 7 miles an hour'
    or 'Winds were northwest at 15 miles an hour, with gusts to 26',
    or 'Winds were calm', or None if data missing.
    """
    dir_abbr, speed, gust = _parse_rwr_wind(wind_raw)
    if speed is None and dir_abbr is None:
        return None
    if speed == 0:
        return "Winds were calm"
    if dir_abbr == "VRB":
        base = f"Winds were variable at {speed} miles an hour"
    elif dir_abbr:
        compass = _COMPASS_SPOKEN.get(dir_abbr, dir_abbr.lower())
        base = f"Winds were {compass} at {speed} miles an hour"
    else:
        return None
    if gust and gust > (speed or 0):
        base += f", with gusts to {gust}"
    return base

# --- centralized from seasonalweather/broadcast/rwr.py ---
def format_station_full(
    station: RwrStation,
    trend_override: Optional[str] = None,
) -> str:
    """
    Full NWR-style spoken observation for an anchor station.
    Matches LWX BMH format: sky / temp+dp / humidity / wind+pressure sentence.
    trend_override: 'rising'/'falling'/'steady' to override product trend char.
    """
    parts: List[str] = []

    # Sky condition
    sky = _sky_spoken(station.sky_raw)
    if sky:
        parts.append(f"At {station.name}, {sky}.")
    else:
        parts.append(f"At {station.name}.")

    # Temperature + dew point
    if station.temp_f is not None:
        temp_line = f"The temperature was {station.temp_f} degrees"
        if station.dewpoint_f is not None:
            temp_line += f", dew point {station.dewpoint_f}"
        parts.append(temp_line + ".")

    # Relative humidity
    if station.rh is not None:
        parts.append(f"Humidity was {station.rh} percent.")

    # Wind + pressure (joined in one sentence like LWX BMH)
    wind_phrase = _format_wind_spoken(station.wind_raw)
    pres_inhg, trend_char = _parse_rwr_pressure(station.pres_raw)
    trend = trend_override or _TREND_SPOKEN.get(trend_char or "", None)

    pres_phrase: Optional[str] = None
    if pres_inhg is not None:
        pres_phrase = f"the barometric pressure was {pres_inhg:.2f} inches"
        if trend:
            pres_phrase += f" and {trend}"

    if wind_phrase and pres_phrase:
        parts.append(f"{wind_phrase} and {pres_phrase}.")
    elif wind_phrase:
        parts.append(f"{wind_phrase}.")
    elif pres_phrase:
        cap = pres_phrase[0].upper() + pres_phrase[1:]
        parts.append(f"{cap}.")

    return " ".join(parts)

# --- centralized from seasonalweather/broadcast/rwr.py ---
def format_station_compact(station: RwrStation) -> str:
    """
    Compact spoken observation: 'At [name], [sky], [temp] degrees.'
    Used for surrounding-area stations after the anchor.
    """
    sky = _sky_spoken(station.sky_raw)
    if station.temp_f is not None and sky:
        return f"At {station.name}, {sky}, {station.temp_f} degrees."
    elif station.temp_f is not None:
        return f"At {station.name}, {station.temp_f} degrees."
    elif sky:
        return f"At {station.name}, {sky}."
    else:
        return f"At {station.name}."

# --- centralized from seasonalweather/broadcast/rwr.py ---
def build_rwr_obs_text(
    product: RwrProduct,
    anchor_names: List[str],
    max_compact_per_section: int = 8,
    intro_prefix: str = "And now for the current observed weather conditions in our area",
    cache: Optional[ObsPressureCache] = None,
    skip_marine: bool = True,
) -> str:
    """
    Assemble NWR-style spoken obs segment from a parsed RwrProduct.

    anchor_names: list of raw station name_raw values (upper-case) that get
                  full-detail treatment. Empty = auto-pick first station in
                  first non-marine section that has temp + pressure.
    max_compact_per_section: max compact stations to read per section.
    intro_prefix: spoken intro before the time/anchor. The time and first
                  station flow naturally from this.
    cache: ObsPressureCache for trend override when product trend char is missing.
    skip_marine: skip marine observation sections (phase 2).
    """
    if not product or not product.sections:
        return ""

    anchors = {n.strip().upper() for n in anchor_names if n.strip()}

    # Auto-derive anchor if none configured
    auto_anchor: Optional[str] = None
    if not anchors:
        for sec in product.sections:
            if sec.is_marine and skip_marine:
                continue
            for st in sec.stations:
                if st.temp_f is not None and st.pres_raw:
                    auto_anchor = st.name_raw
                    anchors = {auto_anchor}
                    break
            if auto_anchor:
                break

    # Build spoken output
    spoken_parts: List[str] = []

    # Intro with time
    time_str = product.issuance_time_str or ""
    if time_str:
        spoken_parts.append(f"{intro_prefix} as of {time_str}.")
    else:
        spoken_parts.append(f"{intro_prefix}.")

    # First pass: anchor station(s) — full detail
    anchor_done: set = set()
    for sec in product.sections:
        if sec.is_marine and skip_marine:
            continue
        for st in sec.stations:
            if st.name_raw in anchors:
                trend_override = None
                if cache is not None:
                    pres_inhg, trend_char = _parse_rwr_pressure(st.pres_raw)
                    if pres_inhg is not None and not trend_char:
                        trend_override = cache.get_trend(st.name_raw, pres_inhg)
                spoken_parts.append(format_station_full(st, trend_override=trend_override))
                anchor_done.add(st.name_raw)
        if anchor_done:
            break  # Anchor section done; compact starts

    if not anchor_done:
        # No anchor found at all — nothing to read
        return ""

    # Second pass: compact surrounding stations, grouped by section
    surroundings_intro_done = False

    for sec in product.sections:
        if sec.is_marine and skip_marine:
            continue

        # Collect compact stations for this section (skip anchors already read)
        compact = [
            st for st in sec.stations
            if st.name_raw not in anchor_done
            and st.temp_f is not None  # skip stations with no usable data
        ][:max_compact_per_section]

        if not compact:
            continue

        # Section intro
        if not surroundings_intro_done:
            spoken_parts.append("Now for some observations from the surrounding area.")
            surroundings_intro_done = True

        # Named section header (if not the first/anchor section)
        section_spoken = _SECTION_SPOKEN.get(sec.title.upper())
        if section_spoken is not None:
            spoken_parts.append(f"In {section_spoken}.")
        else:
            spoken_parts.append("Elsewhere in the region.")

        # Compact station list
        for st in compact:
            spoken_parts.append(format_station_compact(st))

    return " ".join(spoken_parts)

# --- centralized from seasonalweather/broadcast/rwr.py ---
def build_asos_obs_text(
    stations: List[Tuple[str, Dict[str, Any]]],
    anchor_id: str,
    max_compact: int = 8,
    intro_prefix: str = "And now for the current observed weather conditions in our area",
    cache: Optional[ObsPressureCache] = None,
    name_map: Optional[Dict[str, str]] = None,
) -> str:
    """
    Build NWR-style spoken obs from raw ASOS observation dicts.
    Used as fallback when RWR is stale or unavailable.

    stations: list of (station_id, props_dict) pairs, in priority order.
    anchor_id: station ID for the full-detail anchor (first in list if empty).
    """
    if not stations:
        return ""

    anchor = (anchor_id or "").strip().upper() or stations[0][0].upper()

    rwr_stations: List[Tuple[str, RwrStation]] = []
    for sid, props in stations:
        st = asos_to_rwr_station(sid, props, name_map=name_map, cache=cache)
        if st:
            rwr_stations.append((sid.upper(), st))

    if not rwr_stations:
        return ""

    parts: List[str] = [f"{intro_prefix}."]

    # Anchor: full detail
    anchor_done = False
    compact_stns: List[RwrStation] = []
    for sid, st in rwr_stations:
        if sid == anchor and not anchor_done:
            trend_override: Optional[str] = None
            if cache:
                pres_inhg, trend_char = _parse_rwr_pressure(st.pres_raw)
                if pres_inhg is not None and not trend_char:
                    trend_override = cache.get_trend(sid, pres_inhg)
            parts.append(format_station_full(st, trend_override=trend_override))
            anchor_done = True
        elif st.temp_f is not None:
            compact_stns.append(st)

    if not anchor_done:
        # Anchor not found; use first available
        _, first = rwr_stations[0]
        parts.append(format_station_full(first))
        compact_stns = [st for _, st in rwr_stations[1:] if st.temp_f is not None]

    # Compact surrounding stations
    if compact_stns:
        parts.append("Now for some observations from the surrounding area.")
        for st in compact_stns[:max_compact]:
            parts.append(format_station_compact(st))

    return " ".join(parts)

# --- centralized from seasonalweather/broadcast/rwr.py ---
_MARINE_PRES_TREND_SPOKEN: Dict[str, str] = {
    "R": "rising",
    "F": "falling",
    "S": "steady",
}

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _format_marine_wind(st: RwrMarineStation) -> Optional[str]:
    """Return NWR-style wind phrase, or None if no wind data."""
    if st.wind_dir_deg is None or st.wind_spd_kt is None:
        return None
    compass = _degrees_to_compass(float(st.wind_dir_deg))
    compass_spoken = _COMPASS_SPOKEN.get(compass, compass.lower())
    if st.wind_spd_kt == 0:
        return "the wind was calm"
    if st.wind_gust_kt is not None and st.wind_gust_kt > st.wind_spd_kt:
        return (
            f"the wind was {compass_spoken} at {st.wind_spd_kt} knots,"
            f" gusting to {st.wind_gust_kt}"
        )
    return f"the wind was {compass_spoken} at {st.wind_spd_kt} knots"

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _format_marine_station_full(st: RwrMarineStation) -> str:
    """
    Full NWR-style marine obs for an anchor station.
    Includes wind, both temperatures where available, and pressure.
    """
    bits: List[str] = []

    wind = _format_marine_wind(st)
    if wind:
        bits.append(f"At {st.name}, {wind}.")
    else:
        bits.append(f"At {st.name}.")

    # Temperatures — speak both when available, single "temperature" when only one
    if st.air_temp_f is not None and st.sea_temp_f is not None:
        bits.append(
            f"The air temperature was {st.air_temp_f}"
            f" and the water temperature was {st.sea_temp_f}."
        )
    elif st.air_temp_f is not None:
        bits.append(f"The temperature was {st.air_temp_f}.")
    elif st.sea_temp_f is not None:
        bits.append(f"The water temperature was {st.sea_temp_f}.")

    # Pressure with trend — anchor stations only
    if st.pres_mb is not None:
        trend_word = _MARINE_PRES_TREND_SPOKEN.get((st.pres_trend or "").upper(), "")
        pres_bit = f"Barometric pressure {st.pres_mb:.1f} millibars"
        if trend_word:
            pres_bit += f" and {trend_word}"
        bits.append(pres_bit + ".")

    return " ".join(bits)

# --- centralized from seasonalweather/broadcast/rwr.py ---
def _format_marine_station_compact(st: RwrMarineStation) -> str:
    """
    Compact NWR-style marine obs for surrounding stations.
    Wind and temperatures only — no pressure.
    """
    wind = _format_marine_wind(st)

    if wind:
        intro = f"At {st.name}, {wind}."
    else:
        intro = f"At {st.name}."

    if st.air_temp_f is not None and st.sea_temp_f is not None:
        temp = (
            f"The air temperature was {st.air_temp_f}"
            f" and the water temperature was {st.sea_temp_f}."
        )
    elif st.air_temp_f is not None:
        temp = f"The temperature was {st.air_temp_f}."
    elif st.sea_temp_f is not None:
        temp = f"The water temperature was {st.sea_temp_f}."
    else:
        temp = ""

    return f"{intro} {temp}".strip() if temp else intro

# --- centralized from seasonalweather/broadcast/rwr.py ---
def build_marine_obs_text(
    product: RwrProduct,
    max_stations: int = 0,
    anchor_names: Optional[List[str]] = None,
    intro_prefix: str = "Marine observations for the service area",
    name_map: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """
    Assemble NWR-style spoken marine observations segment from
    RwrProduct.marine_stations (populated by parse_rwr's phase-2 marine parser).

    Anchor stations (named in anchor_names) get full detail: wind, both
    temperatures, and barometric pressure.  All other stations get compact
    treatment: wind and temperatures only, matching real NWR marine obs style.

    anchor_names: raw upper-case station names to speak first with full detail,
                  e.g. ["THOMAS PT LIGHT"].  Others follow in product order.
    max_stations: cap on total stations spoken (0 = all).
    name_map:     optional spoken-name overrides applied on top of any names
                  already baked in by parse_rwr.
    """
    if not product or not product.marine_stations:
        return None

    stations = list(product.marine_stations)
    nm = {k.upper(): v for k, v in (name_map or {}).items()}
    anchors = {a.strip().upper() for a in (anchor_names or []) if a.strip()}

    # Apply any name-map overrides from this call
    resolved: List[RwrMarineStation] = []
    for st in stations:
        override = nm.get(st.name_raw)
        if override and override != st.name:
            resolved.append(RwrMarineStation(
                name=override,
                name_raw=st.name_raw,
                obs_time_utc=st.obs_time_utc,
                air_temp_f=st.air_temp_f,
                sea_temp_f=st.sea_temp_f,
                wind_dir_deg=st.wind_dir_deg,
                wind_spd_kt=st.wind_spd_kt,
                wind_gust_kt=st.wind_gust_kt,
                pres_mb=st.pres_mb,
                pres_trend=st.pres_trend,
            ))
        else:
            resolved.append(st)

    # Anchors first (full detail), then the rest in product order (compact)
    anchor_list = [s for s in resolved if s.name_raw in anchors]
    compact_list = [s for s in resolved if s.name_raw not in anchors]

    ordered = anchor_list + compact_list
    cap = max_stations if max_stations > 0 else len(ordered)
    ordered = ordered[:cap]

    if not ordered:
        return None

    parts: List[str] = []

    time_str = product.issuance_time_str or ""
    if time_str:
        parts.append(f"{intro_prefix} as of {time_str}.")
    else:
        parts.append(f"{intro_prefix}.")

    anchor_done: set = set()
    for st in ordered:
        if st.name_raw in anchors and st.name_raw not in anchor_done:
            parts.append(_format_marine_station_full(st))
            anchor_done.add(st.name_raw)
        else:
            parts.append(_format_marine_station_compact(st))

    return " ".join(parts)

# --- public subsystem API ---
class FormatterSubsystem:
    """Controller-facing owner of all source prose-formatting ports."""

    def __init__(
        self,
        *,
        local_tz: ZoneInfo,
        cap_vtec_list: Callable[[object], list[str]],
        vtec_tracks: Callable[[list[str]], list[tuple[str, str]]],
        best_expiry_from_vtec: Callable[[list[str]], dt.datetime | None],
    ) -> None:
        self.cap: CapTextRenderer = CapTextRenderer(
            local_tz=local_tz,
            cap_vtec_list=cap_vtec_list,
            vtec_tracks=vtec_tracks,
            best_expiry_from_vtec=best_expiry_from_vtec,
        )

    def spoken_alert(self, parsed: ParsedProduct, official_text: str) -> SpokenAlert:
        """Format an NWWS/NWS product into the canonical spoken alert shape."""
        return build_spoken_alert(parsed, official_text)

    def nwws_product_script(
        self,
        *,
        product_type: str,
        base_script: str,
        official_text: str,
        vtec: list[str],
        vtec_actions: set[str],
        has_tracks: bool,
        should_full: bool,
        event_text: str,
        area_text: str,
        headline: str,
        local_tz: dt.tzinfo | None = None,
        watch_action: str | None = None,
    ) -> NwwsScriptRenderResult:
        """Apply the canonical NWS/NWWS product narration policy."""
        return render_nws_product_script(
            product_type=product_type,
            base_script=base_script,
            official_text=official_text,
            vtec=vtec,
            vtec_actions=vtec_actions,
            has_tracks=has_tracks,
            should_full=should_full,
            event_text=event_text,
            area_text=area_text,
            headline=headline,
            local_tz=local_tz,
            watch_action=watch_action,
        )

    def ern_relay_script(
        self,
        event: object,
        *,
        same_locations: list[str] | None = None,
        area_text: str = "",
        tz: dt.tzinfo | None = None,
        now_utc: dt.datetime | None = None,
    ) -> str:
        """Format an ERN/GWES SAME relay into spoken prose."""
        return build_ern_relay_script(
            event,
            same_locations=same_locations,
            area_text=area_text,
            tz=tz,
            now_utc=now_utc,
        )

    def ern_start_utc(self, value: str | None, *, now_utc: dt.datetime | None = None) -> dt.datetime | None:
        return same_jday_to_utc(value, now_utc=now_utc)

    def ern_duration_minutes(self, value: str | None) -> int | None:
        return parse_duration_minutes(value)

    def ipaws_script(self, event: object) -> str:
        """Format an IPAWS CAP event after controller policy admits it."""
        return build_ipaws_script(event)

    def now_script(self, product_text: str, *, intro: str) -> str:
        return build_now_script(product_text, intro=intro)

    def pns_state_machine(self, cfg: object, *, tz: ZoneInfo) -> PnsStateMachine:
        return PnsStateMachine(cfg, tz=tz)

    def cap_watch_expansion(self, event: CapAlertEvent) -> str:
        return self.cap.build_watch_expansion_script(event)

    def cap_watch(self, event: CapAlertEvent) -> str:
        return self.cap.build_cap_watch_script(event)

    def cap_full(self, event: CapAlertEvent) -> str:
        return self.cap.build_cap_full_script(event)

    def cap_voice(self, event: CapAlertEvent) -> str:
        return self.cap.build_cap_voice_script(event)

    def cap_watch_action(
        self,
        event: CapAlertEvent,
        vtec_actions: set[str],
        tracks: list[tuple[str, str]],
        watch_number: int | None,
        watch_kind: str,
    ) -> str:
        return self.cap.build_watch_vtec_action_script(event, vtec_actions, tracks, watch_number, watch_kind)

    def cap_prefers_statement_update(self, event: CapAlertEvent | str, vtec_actions: set[str]) -> bool:
        return self.cap.cap_prefers_statement_update_script(event, vtec_actions)

    def cap_statement_action(
        self,
        event: CapAlertEvent,
        vtec_actions: set[str],
        tracks: list[tuple[str, str]],
    ) -> str:
        return self.cap.build_statement_vtec_action_script(event, vtec_actions, tracks)

    def cap_warning_action(
        self,
        event: CapAlertEvent,
        vtec_actions: set[str],
        tracks: list[tuple[str, str]],
    ) -> str:
        return self.cap.build_warning_vtec_action_script(event, vtec_actions, tracks)

    def cap_local_expiry(self, value: str) -> str:
        return self.cap.fmt_local_from_utc_iso(value)


def format_spoken_alert(parsed: ParsedProduct, official_text: str) -> SpokenAlert:
    """Use the canonical alert formatter without constructing a stateful host."""
    return build_spoken_alert(parsed, official_text)

__all__ = [
    'CapTextRenderer',
    'FormatterSubsystem',
    'NwsAlertTextInput',
    'NwwsProductSegment',
    'NwwsScriptRenderResult',
    'ObsPressureCache',
    'Offnt2Product',
    'Offnt2ZoneForecast',
    'PnsDecision',
    'PnsPolicyConfig',
    'PnsStateMachine',
    'PnsSubtypeConfig',
    'RwrMarineStation',
    'RwrProduct',
    'RwrSection',
    'RwrStation',
    'STATE_NAME_FULL',
    'SpokenAlert',
    '_AMPM_ABBR_RE',
    '_AMPM_FIX_RE',
    '_AWIPS_LINE_RE',
    '_COMPASS_DIRS',
    '_COMPASS_SPOKEN',
    '_DEFAULT_STATION_NAMES',
    '_END_PUNCT_RE',
    '_EXPECTED_AWIPS',
    '_EXPECTED_WMO',
    '_EXPIRY_SUMMARY_AMPM_RE',
    '_EXPIRY_SUMMARY_TZ_RE',
    '_ISSUANCE_RE',
    '_LOCATIONS_INCLUDE_RE',
    '_MARINE_AREA_HINTS',
    '_MARINE_PHEN',
    '_MARINE_PRES_RE',
    '_MARINE_PRES_TREND_SPOKEN',
    '_MARINE_TIME_RE',
    '_MARINE_UGC_RE',
    '_MARINE_WIND_RE',
    '_META_SKIP_PREFIXES',
    '_MONTHS',
    '_MONTH_MAP',
    '_NOW_MACHINE_BLOCK_RE',
    '_NOW_MARKER_RE',
    '_NOW_STOP_RE',
    '_NUMBER_RE',
    '_NWS_HEADER_ISSUED_RE',
    '_NWS_ISSUED_RE',
    '_PERIOD_MAP',
    '_PERIOD_RE',
    '_PPA_RE',
    '_PRODUCT_MASTHEAD_RE',
    '_RWR_CITY_HEADER_RE',
    '_RWR_DATA_SKIP_RE',
    '_RWR_ROUTING_RE',
    '_RWR_TIME_RE',
    '_SECTION_SPOKEN',
    '_SEG_ACTION_LABEL_RE',
    '_SEG_AREA_INTRO_RE',
    '_SEG_HEADLINE_RE',
    '_SEG_LOC_RE',
    '_SEG_MACHINE_BLOCK_START_RE',
    '_SEG_META_RE',
    '_SEG_PPA_RE',
    '_SEG_SCOPE_HEADER_RE',
    '_SEG_TIMESTAMP_RE',
    '_SEG_UGC_RE',
    '_SEG_UNTIL_RE',
    '_SEG_VTEC_RE',
    '_SKY_SPOKEN',
    '_SPACE_RE',
    '_SPS_INTRO_LEAD_RE',
    '_STAR_RE',
    '_STATE_ABBRS',
    '_STATE_ABBR_BY_FULL',
    '_STATE_ABBR_RE',
    '_SYNOPSIS_PREFIX_RE',
    '_SYNOPSIS_RE',
    '_TAGS',
    '_TEST_EVENT_CODES',
    '_TREND_SPOKEN',
    '_TZ_ABBR_RE',
    '_TZ_FIX_RE',
    '_TZ_NAME_MAP',
    '_TZ_OFFSETS',
    '_TZ_SPOKEN',
    '_UGC_EXPIRY_RE',
    '_UGC_RE',
    '_WARNING_RE',
    '_WATCH_VTEC_RE',
    '_WCN_AREA_STOP_RE',
    '_WCN_STATE_COUNT_RE',
    '_WMO_RE',
    '_WORD_RE',
    '__all__',
    '_aligned_table_rows',
    '_append_optional',
    '_append_synopsis',
    '_append_zone',
    '_article',
    '_as_tuple_strings',
    '_build_script',
    '_canonical',
    '_clean_county_area_text',
    '_clean_line',
    '_clean_section',
    '_clean_station_name',
    '_clean_wcn_area_name',
    '_collapse_blank_lines',
    '_configured_zones',
    '_contains_all',
    '_contains_any',
    '_degrees_to_compass',
    '_ensure_sentence',
    '_extract_county_area_text',
    '_extract_synopsis',
    '_extract_wcn_area_desc',
    '_extract_wrapped_headline',
    '_find_body_start',
    '_find_cols',
    '_finish_wcn_action_script',
    '_fix_headline_case',
    '_fmt_when',
    '_format_marine_station_compact',
    '_format_marine_station_full',
    '_format_marine_wind',
    '_format_wind_spoken',
    '_has_marine_ugc',
    '_has_marine_vtec',
    '_headline_lines',
    '_identity',
    '_is_issuance_line',
    '_is_routing_line',
    '_issuance_preamble_indices',
    '_iter_param_values',
    '_join_human',
    '_looks_like_all_caps_prose',
    '_looks_like_wcn_area_name',
    '_looks_marine_text',
    '_match_subtype',
    '_normalize_expiry_summary_line',
    '_parse_data_row',
    '_parse_dt',
    '_parse_duration_minutes',
    '_parse_int',
    '_parse_marine_data_row',
    '_parse_marine_section',
    '_parse_rwr_pressure',
    '_parse_rwr_time',
    '_parse_rwr_wind',
    '_parse_vtec_time_utc',
    '_parse_watch_vtec',
    '_parse_zones',
    '_prioritized_zones',
    '_reason_starts_with_event_terminal_scope',
    '_rotated_zones',
    '_routing_sections',
    '_routing_zones',
    '_same_codes_from_event',
    '_same_codes_from_parameters',
    '_same_jday_to_utc',
    '_same_synopsis',
    '_selected_zones',
    '_sentence',
    '_sentence_case_all_caps_prose',
    '_sha1_12',
    '_sky_spoken',
    '_slice_col',
    '_split_nwws_vtec_sections',
    '_split_spoken_candidate',
    '_split_wcn_area_line',
    '_spoken_period_line',
    '_trim_zone_body',
    '_unwrap_soft_wrap',
    '_watch_area_group_phrases',
    '_watch_area_sentence',
    '_watch_label_and_remember',
    '_watch_lifecycle_area_phrase',
    '_watch_section_script_lines',
    '_watch_time_phrase',
    '_watch_vtec_match',
    '_wcn_action_lines',
    '_wcn_action_sections',
    '_wcn_area_match_key',
    '_wcn_lifecycle_action_lines',
    '_wcn_new_action_lines',
    '_within_budget',
    '_word_count',
    '_zone_parts',
    'asos_to_rwr_station',
    'build_asos_obs_text',
    'build_ern_relay_script',
    'build_ipaws_script',
    'build_marine_obs_text',
    'build_now_script',
    'build_nws_full_alert_script',
    'build_nws_voice_alert_script',
    'build_nwws_partial_cancel_script',
    'build_nwws_statement_vtec_action_script',
    'build_nwws_terminal_cancel_expiry_script',
    'build_nwws_watch_action_script',
    'build_nwws_watch_partial_cancel_script',
    'build_nwws_watch_vtec_script',
    'build_rwr_obs_text',
    'build_spoken_alert',
    'build_spoken_alert_full',
    'build_statement_vtec_action_script',
    'build_watch_reminder',
    'build_warning_vtec_action_script',
    'cap_area_label',
    'cap_expiry_summary_line',
    'cap_full_opening_line',
    'cap_is_special_weather_statement',
    'cap_normalize_nws_headline',
    'cap_nwsheadline',
    'cap_prefers_statement_update_script',
    'cap_statement_area_noun',
    'cap_statement_intro',
    'cap_uses_sps_preamble',
    'clean_cap_text',
    'default_pns_subtypes',
    'detect_computer_block_signals',
    'expand_tz_token',
    'expiry_summary_script',
    'extract_now_narrative',
    'extract_nwws_wcn_area_desc',
    'fix_sps_preamble',
    'fmt_local_from_utc_iso',
    'format_spoken_alert',
    'format_station_compact',
    'format_station_full',
    'join_oxford',
    'log',
    'match_nwws_wcn_area_same',
    'nws_header_issued_phrase',
    'parse_cap_area_by_state',
    'parse_duration_minutes',
    'parse_nws_header_issued_dt',
    'parse_nwws_product_segments',
    'parse_offnt2_product',
    'parse_rwr',
    'parse_ugc_expiry_utc',
    'pns_text_same_issuance',
    'policy_from_config',
    'render_nws_product_script',
    'render_nwws_product_script',
    'render_offnt2',
    'same_jday_to_utc',
    'sps_preamble',
    'strip_nws_product_headers',
]
