from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import httpx

from ..alerts.active import ActiveAlert
from ..alerts.nws_api import NWSApi, NWSProduct
from ..tts.tts import clean_for_tts, verbalize_url
from .rwr import (
    ObsPressureCache,
    RwrProduct,
    asos_to_rwr_station,
    build_asos_obs_text,
    build_marine_obs_text,
    build_rwr_obs_text,
    parse_rwr,
)
from .segment_builders import SegmentBuildInput, SegmentCandidate, SegmentSourceEvidence
from .segment_registry import DEFAULT_SEGMENT_REGISTRY, ResolvedSegmentRegistry


@dataclass(frozen=True)
class _ObservationSourceResult:
    text: str
    product: RwrProduct | None
    evidence: SegmentSourceEvidence


def _fmt_time(now: dt.datetime) -> str:
    return now.strftime("%-I:%M %p")


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


def _expand_tz_token(token: str) -> str:
    tok = (token or "").strip()
    if not tok:
        return "local"
    return _TZ_NAME_MAP.get(tok.upper(), tok)


def _short_tz(now: dt.datetime) -> str:
    return _expand_tz_token(now.tzname() or "local")


def station_id_text(ctx: object, station_name: str, service_area_name: str, disclaimer: str) -> str:
    """Build the canonical station-ID wording used by refresh and composition."""
    if getattr(ctx, "health_detached_loop_only", False):
        return (
            getattr(ctx, "health_notice", None)
            or "SeasonalWeather is temporarily unable to receive current National Weather Service information. Please use another weather information source or visit weather.gov for the latest information."
        ).strip()
    base = (
        f"This is the SeasonalNet I P Weather Radio Station, {station_name}, "
        f"with station programming and streaming facilities originating from SeasonalNet, "
        f"providing weather information for {service_area_name}. "
    )
    if getattr(ctx, "mode", "") == "heightened":
        base += (
            "Due to severe weather affecting the service area, normal broadcasts have been "
            "curtailed to bring you the latest severe weather information. "
        )
    return base + disclaimer


# _env_int removed — cycle tuning now flows through CycleBuilder constructor.


_URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_SPACE_RE = re.compile(r"\s+")
_ALL_PUNCT_LINE_RE = re.compile(r"^[\W_]+$")

# CWF/marine product zone routing lines look like:
#   ANZ531-532-533-212300-  or  MDZ010-011-VAZ036-260700-
# The existing _scrub_nws_product_text catches most of these via _CODELINE_RE
# but this dedicated regex guarantees removal as a lightweight pre-pass.
_MARINE_ZONE_ROUTING_RE = re.compile(r"^[A-Z]{2}[Z0-9]\d{2,3}(?:-[A-Z0-9]{3,6})*-\d{6}-?\s*$")


def _strip_marine_routing_lines(text: str) -> str:
    # Pre-pass for CWF / marine product text.
    # Strips zone routing/expiry header lines before the main scrubber.
    # Examples removed:
    #   ANZ531-532-533-534-212300-
    #   MDZ010-011-VAZ036-260700-
    out = []
    for ln in (text or "").splitlines():
        if _MARINE_ZONE_ROUTING_RE.match(ln.strip()):
            continue
        out.append(ln)
    return "\n".join(out)


def _scrub_cwf_product_text(text: str) -> str:
    # CWF-specific pre-pass before the generic NWS scrubber.
    # Strips marine product boilerplate, expands period markers
    # (.SUN... -> 'Sunday.'), advisory banners, direction
    # abbreviations (NW -> northwest), and units (kt -> knots).
    # Also injects spoken anchors for:
    #   - .SYNOPSIS... section headers  -> "The synopsis for the coastal waters in our area."
    #   - Zone name lines               -> "The forecast for {zone name}."

    # --- Synopsis header pre-pass (text-level, before line loop) ---
    # NWS WFOs write synopsis headers in three forms:
    #   Single-line: ".SYNOPSIS FOR THE TIDAL POTOMAC...\n"
    #   Two-line:    ".SYNOPSIS FOR THE COASTAL WATERS FROM SANDY HOOK NJ TO FENWICK\n"
    #                "ISLAND DE AND FOR DELAWARE BAY...\n"
    #   Non-standard two-line (MFL):
    #                ".Synopsis for Jupiter Inlet to Ocean Reef FL out to 60 nm and for\n"
    #                "East Cape Sable to Bonita Beach FL out to 60 nm...\n"
    # The existing line-level drop_re only caught single-line all-caps forms.
    # This pre-pass handles all variants: the continuation line is recognised by
    # (a) starting with a non-. non-blank character, and (b) ending with '...'
    _synopsis_hdr_re = re.compile(
        r"^\.(SYNOPSIS\b[^\n]*)(?:\n(?![.\n])[^\n]*\.{3})?",
        re.IGNORECASE | re.MULTILINE,
    )
    text = _synopsis_hdr_re.sub(
        "The synopsis for the coastal and inland waters in our area.",
        (text or ""),
    )

    # Zone name lines come in the form:
    #   "Tidal Potomac from Key Bridge to Indian Head-"
    #   "Coastal waters from Sandy Hook to Manasquan Inlet NJ out 20 nm-"
    #   "Delaware Bay waters south of East Point NJ to Slaughter Beach DE-"
    # Pattern: starts with a letter (not a digit → excludes "20 nm-" continuations),
    # contains at least one internal space, ends with '-'.
    _zone_name_re = re.compile(r"^[A-Za-z][A-Za-z0-9].*\s+\S.*-\s*$")

    _period_map = {
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
    # Longest keys first so 'SUN NIGHT' matches before 'SUN'.
    _period_keys = sorted(_period_map, key=len, reverse=True)
    _period_re = re.compile(
        r"^\." + r"(" + "|".join(re.escape(k) for k in _period_keys) + r")" + r"\s*\.{3}(.*)",
        re.IGNORECASE,
    )

    # Boilerplate lines to drop entirely.
    _drop_res = [
        re.compile(r"^coastal waters forecast\s*$", re.IGNORECASE),
        re.compile(r"^forecasts of wave heights", re.IGNORECASE),
        re.compile(r"^relative to tidal currents", re.IGNORECASE),
        re.compile(r"^blowing against the tidal", re.IGNORECASE),
        re.compile(r"^waves flat where waters are iced", re.IGNORECASE),
        # NOTE: .SYNOPSIS headers are now handled by the text-level pre-pass above.
    ]

    # Inline advisory banners: ...SMALL CRAFT ADVISORY IN EFFECT...
    _advisory_re = re.compile(r"^\.{3}([A-Z][A-Z ,/]+?)\.{3}\s*$")

    # Direction abbreviations: longest first to avoid N matching in NW etc.
    _dir_map = {
        "NNW": "north-northwest",
        "NNE": "north-northeast",
        "SSW": "south-southwest",
        "SSE": "south-southeast",
        "NW": "northwest",
        "NE": "northeast",
        "SW": "southwest",
        "SE": "southeast",
        "N": "north",
        "S": "south",
        "E": "east",
        "W": "west",
    }
    _dir_re = re.compile(r"\b(NNW|NNE|SSW|SSE|NW|NE|SW|SE|N|S|E|W)\b")

    out: List[str] = []
    for raw_ln in (text or "").replace("\r", "").splitlines():
        ln = raw_ln.strip()

        if not ln:
            out.append("")
            continue

        # Drop known boilerplate lines.
        if any(pat.search(ln) for pat in _drop_res):
            continue

        # Transform inline advisory banners.
        m = _advisory_re.match(ln)
        if m:
            out.append(m.group(1).strip().title() + ".")
            continue

        # Zone name lines: e.g. "Tidal Potomac from Key Bridge to Indian Head-"
        # Recognised by: starts with a letter (not digit, ruling out "20 nm-"
        # continuations), contains at least one internal space, ends with '-'.
        # Emits a spoken zone anchor before the forecast body.
        m = _zone_name_re.match(ln)
        if m:
            zone = ln.rstrip("-").strip()
            out.append(f"The forecast for {zone}.")
            continue

        # Expand .DAY... period markers.
        m = _period_re.match(ln)
        if m:
            key = m.group(1).strip().upper()
            rest = m.group(2).strip()
            spoken = _period_map.get(key, key.title())
            ln = f"{spoken}. {rest}" if rest else f"{spoken}."

        # Collapse remaining ... to ', '
        ln = re.sub(r"\.{3,}", ", ", ln)

        # Expand direction abbreviations.
        ln = _dir_re.sub(lambda mo: _dir_map.get(mo.group(1), mo.group(1)), ln)

        # Expand marine units -- singular before plural so '1 ft' -> '1 foot'
        # not '1 feet', matching how real NWR reads the CWF.
        ln = re.sub(r"\b1\s+kt\b", "1 knot", ln)
        ln = re.sub(r"\b1\s+ft\b", "1 foot", ln)
        ln = re.sub(r"\bkt\b", "knots", ln)
        ln = re.sub(r"\bft\b", "feet", ln)

        out.append(ln)

    return "\n".join(out)


_WMO_HEADER_RE = re.compile(r"^[A-Z]{4}\d{2}\s+[A-Z]{4}\s+\d{6}$")
_ALL_ZERO_RE = re.compile(r"^0{3,}$")
_CODELINE_RE = re.compile(r"^[A-Z0-9/>\-.,\s]{10,}$")

# WFO designators like KLWX/KCTP/KPHI/etc
# NOTE: K[A-Z]{3} also matches airport IDs (KDCA/KBWI/etc), so we whitelist real WFOs we use.
_WFO_ALLOW = {"KLWX", "KCTP", "KPHI"}
_WFO_RE = re.compile(r"\bK[A-Z]{3}\b")


def _last_product_status_line(desc: str, max_chars: int = 260) -> str:
    s = (desc or "").strip()
    if not s:
        return ""

    # Keep this line sane for TTS/logs (avoid giant/ugly strings)
    s = clean_for_tts(s)
    s = _scrub_nws_product_text(s)
    s = _trim_chars(s, max_chars)
    if not s:
        return ""

    m = _WFO_RE.search(s)
    if m and m.group(0) in _WFO_ALLOW:
        return f"Most recently received product from {m.group(0)} was: {s}."

    return f"Most recently received product affecting the service area was: {s}."


# FIPS state code -> USPS postal abbreviation (used to derive CAP "area=" states from SAME/FIPS list)
_FIPS_TO_POSTAL = {
    "01": "AL",
    "02": "AK",
    "04": "AZ",
    "05": "AR",
    "06": "CA",
    "08": "CO",
    "09": "CT",
    "10": "DE",
    "11": "DC",
    "12": "FL",
    "13": "GA",
    "15": "HI",
    "16": "ID",
    "17": "IL",
    "18": "IN",
    "19": "IA",
    "20": "KS",
    "21": "KY",
    "22": "LA",
    "23": "ME",
    "24": "MD",
    "25": "MA",
    "26": "MI",
    "27": "MN",
    "28": "MS",
    "29": "MO",
    "30": "MT",
    "31": "NE",
    "32": "NV",
    "33": "NH",
    "34": "NJ",
    "35": "NM",
    "36": "NY",
    "37": "NC",
    "38": "ND",
    "39": "OH",
    "40": "OK",
    "41": "OR",
    "42": "PA",
    "44": "RI",
    "45": "SC",
    "46": "SD",
    "47": "TN",
    "48": "TX",
    "49": "UT",
    "50": "VT",
    "51": "VA",
    "53": "WA",
    "54": "WV",
    "55": "WI",
    "56": "WY",
    "60": "AS",
    "66": "GU",
    "69": "MP",
    "72": "PR",
    "78": "VI",
}


def _areas_from_same_fips(same_fips_all: List[str]) -> List[str]:
    """
    Derive NWS CAP 'area=' state list from our configured SAME/FIPS allowlist.

    SAME county code format: PSSCCC
      - P = subdivision (0=entire county/zone)
      - SS = state FIPS (2 digits)
      - CCC = county/city FIPS (3 digits)

    Marine SAME codes in our config start with "07" (e.g. 073532) and are NOT state/county zones.
    We skip those for CAP area derivation.
    """
    out: set[str] = set()
    for s in same_fips_all or []:
        s = str(s).strip()
        if len(s) != 6 or not s.isdigit():
            continue
        if s.startswith("07"):  # marine codes
            continue
        st = _FIPS_TO_POSTAL.get(s[1:3])
        if st:
            out.add(st)
    return sorted(out)


def _scrub_nws_product_text(text: str) -> str:
    t = (text or "").replace("\r", "")
    out_lines: list[str] = []

    for raw in t.splitlines():
        line = raw.strip()

        if not line:
            out_lines.append("")
            continue

        if line in {"&&", "$$"}:
            continue

        if _ALL_PUNCT_LINE_RE.match(line):
            continue

        if _WMO_HEADER_RE.match(line):
            continue

        if _ALL_ZERO_RE.match(line):
            continue

        if _URL_RE.search(line):
            line = _URL_RE.sub(lambda m: " " + verbalize_url(m.group(0)) + " ", line)
        if _EMAIL_RE.search(line):
            line = _EMAIL_RE.sub(" ", line)

        line = _SPACE_RE.sub(" ", line).strip(" -:;()[]<>")

        if not line:
            continue

        if _CODELINE_RE.match(line) and not any(ch.islower() for ch in line):
            # Prose gate: 3+ purely alphabetic tokens means this is all-caps NWS prose
            # (e.g. SYN product body: "HIGH PRESSURE WILL REMAIN OVER THE REGION."),
            # NOT a code/routing line.  Code lines (TAF, WMO headers, AWIPS IDs) have
            # at most 1-2 purely alphabetic tokens; the rest are digit-heavy abbreviations.
            if sum(1 for w in line.split() if w.isalpha()) >= 3:
                pass  # all-caps prose — keep it
            else:
                continue

        out_lines.append(line)

    cleaned: list[str] = []
    blank = False
    for l in out_lines:
        if l == "":
            if blank:
                continue
            blank = True
            cleaned.append("")
        else:
            blank = False
            cleaned.append(l)

    return "\n".join(cleaned).strip()


def _trim_chars(text: str, max_chars: Optional[int]) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    if max_chars is None or int(max_chars) <= 0:
        return s
    if len(s) <= max_chars:
        return s

    cut = s[:max_chars].rsplit(" ", 1)[0].rstrip()
    if len(cut) < int(max_chars * 0.6):
        cut = s[:max_chars].rstrip()

    return cut + "…"


def _extract_afd_synopsis(raw: str) -> str:
    """
    Extract ONLY the AFD synopsis section, if present.
    AFD headings look like:
      .SYNOPSIS...
      .NEAR TERM /THROUGH TONIGHT/...
      .SHORT TERM /.../...
    """
    txt = (raw or "").replace("\r", "")
    lines = txt.splitlines()

    in_syn = False
    buf: list[str] = []

    for ln in lines:
        s = ln.strip("\n")

        if not in_syn:
            if s.strip().upper().startswith(".SYNOPSIS"):
                in_syn = True
            continue

        # Stop at next AFD section header
        ss = s.strip()
        if ss.startswith(".") and "..." in ss and ss.upper() == ss.upper():
            # Example: ".NEAR TERM /THROUGH TONIGHT/..."
            # This reliably marks the end of synopsis content.
            break

        buf.append(s)

    out = "\n".join(buf).strip()
    return out


@dataclass
class CycleContext:
    mode: str  # "normal" | "heightened"
    last_heightened_ago: Optional[str]
    last_product_desc: Optional[str]
    health_mode: str = "normal"
    health_notice: Optional[str] = None
    health_status_line: Optional[str] = None
    health_detached_loop_only: bool = False
    active_alerts: tuple[ActiveAlert, ...] = ()


_ALERT_COUNT_WORDS = {
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
}


def _pluralize_station_status_label(label: str) -> str:
    replacements = {
        "advisory": "advisories",
        "emergency": "emergencies",
        "watch": "watches",
        "warning": "warnings",
        "statement": "statements",
        "outlook": "outlooks",
        "message": "messages",
    }
    low = label.casefold()
    for singular, plural in replacements.items():
        if low.endswith(singular):
            suffix = label[-len(singular) :]
            spoken_plural = plural.capitalize() if suffix[:1].isupper() else plural
            return label[: -len(singular)] + spoken_plural
    return label if low.endswith("s") else label + "s"


def _station_status_alert_summary(
    alerts: Sequence[ActiveAlert],
    *,
    max_groups: int = 8,
) -> tuple[str, int]:
    grouped: dict[str, list[Any]] = {}
    included_count = 0

    for alert in alerts:
        if (alert.source or "").strip().upper() == "PNS_CYCLE":
            continue

        included_count += 1
        label = str(alert.event or alert.headline or alert.code or "Active alert")
        label = _SPACE_RE.sub(" ", label).strip().rstrip(".") or "Active alert"
        if (
            alert.watch_number is not None
            and "watch" in label.casefold()
            and not re.search(r"\bnumber\s+\d+\b", label, flags=re.IGNORECASE)
        ):
            label = f"{label} number {alert.watch_number}"

        key = label.casefold()
        if key not in grouped:
            grouped[key] = [label, 1]
        elif " number " not in key:
            grouped[key][1] += 1

    groups = list(grouped.values())
    visible = groups[: max(1, int(max_groups))]
    rendered: list[str] = []
    for label, count in visible:
        if count == 1:
            rendered.append(str(label))
        else:
            count_text = _ALERT_COUNT_WORDS.get(int(count), str(count))
            rendered.append(f"{count_text} {_pluralize_station_status_label(str(label))}")

    if len(groups) > len(visible):
        remaining = sum(int(count) for _label, count in groups[len(visible) :])
        count_text = _ALERT_COUNT_WORDS.get(remaining, str(remaining))
        rendered.append(f"{count_text} additional active alerts")

    return "; ".join(rendered), included_count


def build_station_status_text(
    ctx: CycleContext,
    active_alerts: Sequence[ActiveAlert],
    *,
    last_product_max_chars: int = 260,
) -> str:
    mode = "heightened" if (ctx.mode or "").strip().casefold() == "heightened" else "normal"
    status_bits: list[str] = [f"SeasonalWeather is currently operating in {mode} broadcast mode."]

    if mode == "heightened" and ctx.last_heightened_ago:
        status_bits.append(f"Heightened mode was activated {ctx.last_heightened_ago} ago.")

    if ctx.last_product_desc:
        line = _last_product_status_line(
            ctx.last_product_desc,
            max_chars=last_product_max_chars,
        )
        if line:
            status_bits.append(line)

    health_status_line = (ctx.health_status_line or "").strip()
    if health_status_line:
        status_bits.append(health_status_line)

    alert_summary, alert_count = _station_status_alert_summary(active_alerts)
    if alert_count == 1:
        status_bits.append(f"The active alert in the service area is {alert_summary}.")
    elif alert_count > 1:
        status_bits.append(f"The active alerts in the service area are: {alert_summary}.")
    else:
        status_bits.append("No active alerts are currently being tracked for the service area.")

    return "And now, the station status and active alerts. " + " ".join(status_bits)


@dataclass(frozen=True)
class CycleSegment:
    key: str
    title: str
    text: str


# --- Broadcast text helpers (HWO/OBS formatting) ---

_HWO_ISSUED_RE = re.compile(
    r"^(?P<hm>\d{3,4})\s*(?P<ampm>AM|PM)\s*(?P<tz>[A-Z]{2,4})\s*"
    r"(?P<dow>[A-Za-z]{3})\s*(?P<mon>[A-Za-z]{3})\s*(?P<day>\d{1,2})\s*(?P<year>\d{4})$"
)

_DOW_FULL = {
    "Mon": "Monday",
    "Tue": "Tuesday",
    "Wed": "Wednesday",
    "Thu": "Thursday",
    "Fri": "Friday",
    "Sat": "Saturday",
    "Sun": "Sunday",
}

# _parse_kv_env removed — obs aliases now come from CycleBuilder constructor.


def _parse_kv_env(key: str) -> Dict[str, str]:
    """Legacy shim — kept so any callers outside CycleBuilder still compile."""
    raw = (os.environ.get(key, "") or "").strip()
    if not raw:
        return {}
    out: Dict[str, str] = {}
    parts = re.split(r"[;,]", raw)
    for p in parts:
        s = p.strip()
        if not s:
            continue
        if ":" in s:
            k, v = s.split(":", 1)
        elif "=" in s:
            k, v = s.split("=", 1)
        else:
            continue
        k = k.strip().upper()
        v = v.strip()
        if k and v:
            out[k] = v
    return out


def _hwo_issued_phrase(raw: str) -> Optional[str]:
    """
    Pull "Issued at ..." from the product header line like:
      1002 AM EST Thu Mar 5 2026
    """
    txt = (raw or "").replace("\r", "")
    for ln in txt.splitlines():
        line = ln.strip()
        m = _HWO_ISSUED_RE.match(line)
        if not m:
            continue
        hm = m.group("hm")
        ampm = m.group("ampm")
        dow = m.group("dow").title()

        # Convert 1002 -> 10:02, 902 -> 9:02
        if len(hm) == 3:
            h = int(hm[0])
            mins = hm[1:]
        else:
            h = int(hm[:2])
            mins = hm[2:]
        hhmm = f"{h}:{mins.zfill(2)} {ampm}"
        dow_full = _DOW_FULL.get(dow, dow)
        return f"Issued at {hhmm} on {dow_full}."
    return None


def _simplify_hwo(raw: str) -> str:
    """
    Convert raw HWO product text into something closer to NWR-style phrasing.
    Keeps Day One / Days Two Through Seven / Spotter lines, deduped.
    """
    issued = _hwo_issued_phrase(raw)
    cleaned = _scrub_nws_product_text(clean_for_tts(raw))

    sections: Dict[str, List[str]] = {"day1": [], "day2to7": [], "spotter": []}
    cur: Optional[str] = None

    for ln in cleaned.splitlines():
        s = ln.strip()
        if not s:
            continue

        low = s.lower()

        # Drop the noisy top banner + repeated area boilerplate
        if low.startswith("hazardous weather outlook"):
            continue
        if low.startswith("national weather service"):
            continue
        if _HWO_ISSUED_RE.match(s):
            continue
        if low.startswith("this hazardous weather outlook is for"):
            continue

        if low.startswith("day one"):
            cur = "day1"
            continue
        if low.startswith("days two through seven"):
            cur = "day2to7"
            continue
        if low.startswith("spotter information statement"):
            cur = "spotter"
            continue

        if cur:
            sections[cur].append(s)

    def dedupe(lines: List[str]) -> List[str]:
        seen: set[str] = set()
        out: List[str] = []
        for x in lines:
            key = x.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(x.strip())
        return out

    day1 = dedupe(sections["day1"])
    day2 = dedupe(sections["day2to7"])
    spot = dedupe(sections["spotter"])

    parts: List[str] = []
    if issued:
        parts.append(issued)

    if day1:
        parts.append("Day one. " + " ".join(day1))
    if day2:
        parts.append("Days two through seven. " + " ".join(day2))
    if spot:
        parts.append("Spotter information. " + " ".join(spot))

    return _SPACE_RE.sub(" ", " ".join(parts)).strip()


def _spc_threats_phrase(torn_prob: int, hail_prob: int, wind_prob: int, *, severity_dn: int) -> str:
    """
    Build a short, broadcast-friendly threat phrase from SPC probabilistic layers.

    Probabilities are typically percent (e.g., 2, 5, 15, 30).
    """
    threats: List[str] = []

    if torn_prob >= 2:
        threats.append("a few tornadoes")
    if hail_prob >= 5:
        threats.append("large hail")
    if wind_prob >= 5:
        threats.append("damaging winds")

    if threats:
        if len(threats) == 1:
            return threats[0].capitalize()
        if len(threats) == 2:
            return f"{threats[0].capitalize()} and {threats[1]}"
        return f"{threats[0].capitalize()}, {threats[1]}, and {threats[2]}"

    if severity_dn >= 6:
        return "Large hail, damaging winds, and a few tornadoes"
    if severity_dn >= 4:
        return "Large hail and damaging winds"
    return "Isolated severe storms"


class CycleBuilder:
    def __init__(
        self,
        api: NWSApi,
        tz_name: str,
        obs_stations: List[str],
        reference_points: List[Tuple[float, float, str]],
        same_fips_all: List[str],
        cycle_cfg=None,
        registry: ResolvedSegmentRegistry | None = None,
        work_dir: str = "/var/lib/seasonalweather",
    ) -> None:
        self.api = api
        self.tz = ZoneInfo(tz_name)
        self.obs_stations = obs_stations
        self.points = reference_points
        self.same_fips = set(same_fips_all)
        self._cycle_cfg = cycle_cfg  # CycleConfig | None — None falls back to hardcoded defaults
        self._registry = registry or DEFAULT_SEGMENT_REGISTRY.resolve_for_cycle(cycle_cfg)
        # Pressure cache for RWR trend derivation (survives restarts)
        import os as _os

        _cache_path = _os.path.join(work_dir, "obs_pressure_cache.json")
        _rwr = cycle_cfg.rwr if cycle_cfg else None
        self._pressure_cache = ObsPressureCache(
            path=_cache_path,
            max_hours=float(_rwr.pressure_cache_hours) if _rwr else 3.0,
            trend_threshold_inhg=float(_rwr.pressure_trend_threshold_inhg) if _rwr else 0.02,
        )

        # Observation station naming
        self._obs_aliases: Dict[str, str] = dict(cycle_cfg.obs.aliases) if cycle_cfg else {}
        self._obs_name_cache: Dict[str, str] = {}

        # caches for SPC/CWA lookups (best-effort)
        self._wfo_geom_cache: Dict[str, Dict[str, Any]] = {}
        self._arcgis_layer_cache: Dict[str, int] = {}

        # Derive CAP "area=" list from SAME/FIPS list (keeps PA/PHI/CTP etc automatically in sync)
        self.alert_areas = _areas_from_same_fips(same_fips_all)
        if not self.alert_areas:
            # fail-safe (should never happen with a real config)
            self.alert_areas = ["MD", "VA", "DC", "WV"]

    async def _fetch_station_name(self, station_id: str) -> Optional[str]:
        st = (station_id or "").strip().upper()
        if not st:
            return None
        try:
            url = f"https://api.weather.gov/stations/{st}"
            async with httpx.AsyncClient(
                timeout=3.0,
                headers={"User-Agent": "SeasonalWeather/SeasonalNet"},
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json() or {}
            name = ((data.get("properties") or {}).get("name") or "").strip()
            if not name:
                return None
            name = clean_for_tts(name)
            name = _scrub_nws_product_text(name)
            return name or None
        except Exception:
            return None

    async def _obs_label(self, station_id: str) -> str:
        st = (station_id or "").strip().upper()
        if not st:
            return "Unknown station"
        if st in self._obs_aliases:
            return self._obs_aliases[st]
        if st in self._obs_name_cache:
            return self._obs_name_cache[st]

        name = await self._fetch_station_name(st)
        if name:
            self._obs_name_cache[st] = name
            return name

        self._obs_name_cache[st] = st
        return st

    def _product_max_chars(self, kind: str, mode: str) -> Optional[int]:
        """Return per-product hard character caps.

        Heightened mode no longer hard-truncates products.  The conductor
        handles severe-weather focus by postponing routine segments instead
        of clipping words out of spoken products.
        """
        k = (kind or "").strip().upper()
        m = (mode or "normal").strip().lower()

        if m == "heightened":
            return None

        if k == "HWO":
            return self._cycle_cfg.hwo.max_chars_normal if self._cycle_cfg else 0

        if k == "AFD":
            return self._cycle_cfg.afd.max_chars_normal if self._cycle_cfg else 0

        if k in {"SYN", "SYNOPSIS"}:
            return self._cycle_cfg.syn.max_chars_normal if self._cycle_cfg else 1500

        if k == "CWF":
            return self._cycle_cfg.cwf.max_chars_normal if self._cycle_cfg else 2000

        return None

    async def _fetch_product(self, kind: str, office: str) -> tuple[NWSProduct, SegmentSourceEvidence] | None:
        try:
            pid = await self.api.latest_product_id(kind, office)
            if not pid:
                return None
            prod = await self.api.get_product(pid)
            if not prod or not prod.product_text:
                return None
            fetched_at = dt.datetime.now(dt.UTC)
            return prod, SegmentSourceEvidence(
                source_name="nws",
                product_identifier=getattr(prod, "product_id", None) or pid,
                product_type=getattr(prod, "product_type", None) or kind,
                issuing_office=getattr(prod, "wfo", None) or office,
                issuance_time=getattr(prod, "issuance_time", None),
                fetched_at=fetched_at,
                source_reference=f"https://api.weather.gov/products/{getattr(prod, 'product_id', None) or pid}",
            )
        except Exception:
            return None

    async def _fetch_product_text(self, kind: str, office: str) -> Optional[str]:
        fetched = await self._fetch_product(kind, office)
        return fetched[0].product_text if fetched else None

    @staticmethod
    def _clean_hwo_body(raw: str) -> str:
        raw = raw.replace("\r", "")
        issued = _hwo_issued_phrase(raw)
        lines = raw.splitlines()

        def norm(value: str) -> str:
            return (value or "").lstrip("\ufeff").strip()

        def is_blank(index: int) -> bool:
            return index >= len(lines) or not norm(lines[index])

        index = 0
        while index < len(lines) and is_blank(index):
            index += 1
        banner = index
        while banner < len(lines) and "hazardous weather outlook" not in norm(lines[banner]).lower():
            banner += 1
        if banner < len(lines):
            index = banner + 1
            while index < len(lines) and is_blank(index):
                index += 1
            if index < len(lines) and norm(lines[index]).lower().startswith("national weather service"):
                index += 1
            while index < len(lines) and is_blank(index):
                index += 1
            if index < len(lines) and _HWO_ISSUED_RE.match(norm(lines[index])):
                index += 1
            while index < len(lines) and is_blank(index):
                index += 1
        body = "\n".join(lines[index:]).strip()
        if issued:
            body = f"{issued}\n{body}" if body else issued
        return _scrub_nws_product_text(clean_for_tts(body))

    async def _build_synopsis_text(self, ctx: CycleContext) -> tuple[str, SegmentSourceEvidence] | str | None:
        """
        "Synopsis" segment source order:
          1) SYN product if available (some offices: BOU, ABR, GJT, MRX)
          2) RWS (Regional Weather Summary) — what LWX and many eastern offices publish;
             this is the same product real NWR transmitters read for their synopsis segment
          3) AFD .SYNOPSIS only (fallback for offices with neither SYN nor RWS)
          4) None (fail closed; never read full ZFP by accident)
        """
        # 1) Dedicated synopsis product (a few offices only)
        syn_fetched = await self._fetch_product("SYN", "LWX")
        if syn_fetched:
            syn_product, syn_evidence = syn_fetched
            syn_clean = clean_for_tts(syn_product.product_text)
            syn_clean = _trim_chars(syn_clean, self._product_max_chars("SYN", ctx.mode))
            syn_clean = _scrub_nws_product_text(syn_clean)
            if syn_clean:
                return syn_clean, syn_evidence

        # 2) Regional Weather Summary — broadcast-ready prose, same format real NWR uses
        rws_fetched = await self._fetch_product("RWS", "LWX")
        if rws_fetched:
            rws_product, rws_evidence = rws_fetched
            rws_clean = clean_for_tts(rws_product.product_text)
            rws_clean = _trim_chars(rws_clean, self._product_max_chars("SYN", ctx.mode))
            rws_clean = _scrub_nws_product_text(rws_clean)
            if rws_clean:
                return rws_clean, rws_evidence

        # 3) AFD synopsis extraction fallback
        afd_fetched = await self._fetch_product("AFD", "LWX")
        if afd_fetched:
            afd_product, afd_evidence = afd_fetched
            syn = _extract_afd_synopsis(afd_product.product_text)
            if syn:
                syn_clean = clean_for_tts(syn)
                syn_clean = _trim_chars(syn_clean, self._product_max_chars("SYNOPSIS", ctx.mode))
                syn_clean = _scrub_nws_product_text(syn_clean)
                if syn_clean:
                    return syn_clean, afd_evidence

        return None

    async def _arcgis_get_json(self, url: str, params: Mapping[str, Any], timeout_s: float) -> Optional[Dict[str, Any]]:
        """
        ArcGIS REST helper.

        IMPORTANT:
          - If we pass a polygon geometry, the querystring can be huge; GET requests may be truncated.
          - Use POST (form-encoded) whenever the request includes a 'geometry' parameter.
        """
        try:
            async with httpx.AsyncClient(
                timeout=timeout_s,
                headers={"User-Agent": "SeasonalWeather/SeasonalNet"},
            ) as client:
                p = dict(params)
                if "geometry" in p:
                    r = await client.post(url, data=p)
                else:
                    r = await client.get(url, params=p)
                r.raise_for_status()
                payload = r.json()
                return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    async def _arcgis_find_layer_id(
        self, base_url: str, want_keywords: Iterable[str], timeout_s: float
    ) -> Optional[int]:
        """
        Fetch MapServer metadata and find the first layer whose name contains all keywords.
        Caches results per (base_url, keywords).
        """
        key = f"{base_url}|" + "|".join(k.lower().strip() for k in want_keywords if k)
        if key in self._arcgis_layer_cache:
            return self._arcgis_layer_cache[key]

        svc = await self._arcgis_get_json(base_url, {"f": "pjson"}, timeout_s)
        layers = (svc or {}).get("layers") or []
        want = [k.lower().strip() for k in want_keywords if k and k.strip()]
        for layer in layers:
            name = str((layer or {}).get("name") or "").lower()
            if name and all(k in name for k in want):
                lid = (layer or {}).get("id")
                if isinstance(lid, int):
                    self._arcgis_layer_cache[key] = lid
                    return lid
        return None

    async def _arcgis_query(
        self,
        base_url: str,
        layer_id: int,
        where: str,
        *,
        geometry: Optional[Dict[str, Any]] = None,
        out_fields: str = "*",
        return_geometry: bool = False,
        timeout_s: float = 6.0,
    ) -> List[Dict[str, Any]]:
        url = f"{base_url.rstrip('/')}/{layer_id}/query"
        params: Dict[str, Any] = {
            "f": "pjson",
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true" if return_geometry else "false",
            "outSR": 4326,
        }
        if geometry:
            params.update(
                {
                    "geometry": json.dumps(geometry),
                    "geometryType": "esriGeometryPolygon",
                    "spatialRel": "esriSpatialRelIntersects",
                    "inSR": 4326,
                }
            )

        data = await self._arcgis_get_json(url, params, timeout_s)
        feats = (data or {}).get("features") or []
        out: List[Dict[str, Any]] = []
        for f in feats:
            if isinstance(f, dict):
                out.append(f)
        return out

    async def _wfo_cwa_geometry(self, wfo: str, timeout_s: float) -> Optional[Dict[str, Any]]:
        """
        Returns an ESRI polygon geometry (wkid 4326) for a WFO CWA, best-effort.
        """
        wfo = (wfo or "").strip().upper()
        if not wfo:
            return None
        if wfo in self._wfo_geom_cache:
            return self._wfo_geom_cache[wfo]

        ref_base = (
            "https://mapservices.weather.noaa.gov/static/rest/services/nws_reference_maps/nws_reference_map/MapServer"
        )
        layer_id = (
            await self._arcgis_find_layer_id(ref_base, ["county warning area"], timeout_s)
            or await self._arcgis_find_layer_id(ref_base, ["cwa"], timeout_s)
            or await self._arcgis_find_layer_id(ref_base, ["weather forecast office"], timeout_s)
            or await self._arcgis_find_layer_id(ref_base, ["wfo"], timeout_s)
        )
        if layer_id is None:
            return None

        field_candidates = ["WFO", "WFO_ID", "WFOID", "CWA", "OFFICE", "SITE", "SITEID", "ID"]
        for fld in field_candidates:
            feats = await self._arcgis_query(
                ref_base, layer_id, f"{fld}='{wfo}'", return_geometry=True, timeout_s=timeout_s
            )
            if feats:
                geom = (feats[0] or {}).get("geometry")
                if isinstance(geom, dict) and ("rings" in geom):
                    if "spatialReference" not in geom:
                        geom["spatialReference"] = {"wkid": 4326}
                    self._wfo_geom_cache[wfo] = geom
                    return geom
        return None

    def _spc_dn_to_code(self, dn: int) -> str:
        if dn >= 8:
            return "HIGH"
        if dn >= 6:
            return "MDT"
        if dn >= 5:
            return "ENH"
        if dn >= 4:
            return "SLGT"
        if dn >= 3:
            return "MRGL"
        if dn >= 2:
            return "TSTM"
        return ""

    def _spc_code_to_spoken(self, code: str) -> str:
        c = (code or "").strip().upper()
        return {
            "MRGL": "marginal",
            "SLGT": "slight",
            "ENH": "enhanced",
            "MDT": "moderate",
            "HIGH": "high",
            "TSTM": "general thunderstorm",
        }.get(c, c.lower())

    async def _spc_max_risk_dn(self, day: int, wfos: List[str], timeout_s: float) -> int:
        """
        Return the maximum categorical risk DN intersecting any WFO CWA.
        Best-effort: returns 0 on failure.

        ArcGIS attribute keys vary in case; NOAA commonly returns:
          - dn, label, label2
        """
        spc_base = "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_wx_outlks/MapServer"
        layer_id = await self._arcgis_find_layer_id(spc_base, [f"day {day}", "categorical"], timeout_s)
        if layer_id is None:
            return 0

        # Map both codes and words to DN
        code_to_dn = {"TSTM": 2, "MRGL": 3, "SLGT": 4, "ENH": 5, "MDT": 6, "HIGH": 8}
        word_to_dn = {
            "GENERAL": 2,
            "THUNDER": 2,
            "MARGINAL": 3,
            "SLIGHT": 4,
            "ENHANCED": 5,
            "MODERATE": 6,
            "HIGH": 8,
        }

        def get_attr(attrs: dict, *names: str):
            for n in names:
                if n in attrs:
                    return attrs.get(n)
            for n in names:
                ln = n.lower()
                if ln in attrs:
                    return attrs.get(ln)
            return None

        max_dn = 0
        for wfo in wfos:
            geom = await self._wfo_cwa_geometry(wfo, timeout_s)
            if not geom:
                continue

            feats = await self._arcgis_query(
                spc_base, layer_id, "1=1", geometry=geom, return_geometry=False, timeout_s=timeout_s
            )

            for f in feats:
                attrs = (f or {}).get("attributes") or {}

                # Prefer explicit dn if present
                dn = get_attr(attrs, "DN", "dn")
                if isinstance(dn, (int, float)):
                    max_dn = max(max_dn, int(dn))
                    continue

                # Otherwise parse label/label2
                lab = get_attr(
                    attrs, "LABEL", "label", "LABEL2", "label2", "CAT", "cat", "CATEGORY", "category", "RISK", "risk"
                )
                if isinstance(lab, str) and lab.strip():
                    u = lab.strip().upper()

                    # Try codes first
                    for code, dnv in code_to_dn.items():
                        if code in u:
                            max_dn = max(max_dn, dnv)
                            break
                    else:
                        # Then words (e.g., "Marginal", "Slight", etc.)
                        for word, dnv in word_to_dn.items():
                            if word in u:
                                max_dn = max(max_dn, dnv)
                                break

        return max_dn

    async def _spc_max_prob(self, day: int, hazard: str, wfos: List[str], timeout_s: float) -> int:
        """
        Max probability (percent) for a hazard (tornado/hail/wind) intersecting any WFO CWA.
        Returns 0 on failure.

        NOAA ArcGIS layers often store the probability in 'dn' (lowercase).
        """
        spc_base = "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_wx_outlks/MapServer"
        haz = (hazard or "").strip().lower()
        if haz not in {"tornado", "hail", "wind"}:
            return 0

        layer_id = await self._arcgis_find_layer_id(spc_base, [f"day {day}", haz], timeout_s)
        if layer_id is None and haz == "tornado":
            layer_id = await self._arcgis_find_layer_id(spc_base, [f"day {day}", "torn"], timeout_s)
        if layer_id is None:
            return 0

        def get_attr(attrs: dict, *names: str):
            for n in names:
                if n in attrs:
                    return attrs.get(n)
            for n in names:
                ln = n.lower()
                if ln in attrs:
                    return attrs.get(ln)
            return None

        max_p = 0
        for wfo in wfos:
            geom = await self._wfo_cwa_geometry(wfo, timeout_s)
            if not geom:
                continue
            feats = await self._arcgis_query(
                spc_base, layer_id, "1=1", geometry=geom, return_geometry=False, timeout_s=timeout_s
            )
            for f in feats:
                attrs = (f or {}).get("attributes") or {}
                v = get_attr(
                    attrs,
                    "PROB",
                    "prob",
                    "PROBABILITY",
                    "probability",
                    "PERCENT",
                    "percent",
                    "VALUE",
                    "value",
                    "VAL",
                    "val",
                    "DN",
                    "dn",
                )
                if isinstance(v, (int, float)):
                    max_p = max(max_p, int(v))
        return max_p

    async def _build_spc_outlook_text(self, ctx: CycleContext, now: dt.datetime) -> Optional[str]:
        """
        Optional SPC convective outlook readout (Day 1-3) scoped to configured WFO CWAs.

        Enabled with:
          SEASONAL_CYCLE_SPC_ENABLE=1
          SEASONAL_CYCLE_SPC_WFOS=LWX,CTP,PHI
          SEASONAL_CYCLE_SPC_MIN_DN=3  (3=MRGL)
          SEASONAL_CYCLE_SPC_DAYS=3    (1..3)
        """
        if not self._registry.enabled("spc"):
            return None

        wfos = list(self._cycle_cfg.spc.wfos) if self._cycle_cfg else ["LWX"]
        if not wfos:
            wfos = ["LWX"]

        days = 1 if ctx.mode == "heightened" else max(1, min(3, self._cycle_cfg.spc.days if self._cycle_cfg else 3))
        min_dn = self._cycle_cfg.spc.min_dn if self._cycle_cfg else 3
        try:
            timeout_s = float(self._cycle_cfg.spc.timeout_s if self._cycle_cfg else 6.0)
        except Exception:
            timeout_s = 6.0

        lines: List[str] = []

        for day in range(1, days + 1):
            dn = await self._spc_max_risk_dn(day, wfos, timeout_s)
            if dn < min_dn:
                continue

            code = self._spc_dn_to_code(dn)
            spoken = self._spc_code_to_spoken(code)

            if day == 1:
                torn = await self._spc_max_prob(1, "tornado", wfos, timeout_s)
                hail = await self._spc_max_prob(1, "hail", wfos, timeout_s)
                wind = await self._spc_max_prob(1, "wind", wfos, timeout_s)
                threats = _spc_threats_phrase(torn, hail, wind, severity_dn=dn)
                lines.append(
                    f"For today's convective outlook in our service area, there is a {spoken} risk of severe thunderstorms. {threats} will be possible."
                )
            elif day == 2:
                torn = await self._spc_max_prob(2, "tornado", wfos, timeout_s)
                hail = await self._spc_max_prob(2, "hail", wfos, timeout_s)
                wind = await self._spc_max_prob(2, "wind", wfos, timeout_s)
                threats = _spc_threats_phrase(torn, hail, wind, severity_dn=dn)
                lines.append(
                    f"For tomorrow's convective outlook in our service area, there is a {spoken} risk of severe thunderstorms. {threats} will be possible."
                )
            elif day == 3:
                d3 = now + dt.timedelta(days=2)
                lines.append(f"For {d3.strftime('%A')}, a {spoken} risk of severe thunderstorms is possible.")

        if not lines:
            return None

        return (
            "And now, for the Storm Prediction Center's convective outlook for severe thunderstorms in our area. "
            + " ".join(lines)
        )

    async def _build_cwf_text(self, ctx: CycleContext) -> Optional[str]:
        built = await self._build_cwf_text_with_evidence(ctx)
        return built[0] if built else None

    async def _build_cwf_text_with_evidence(self, ctx: CycleContext) -> tuple[str, SegmentSourceEvidence] | None:
        # Fetch and scrub the Coastal Waters Forecast for this cycle.
        # Enabled when cycle.cwf.enabled = true.
        # Tries each configured office in order; returns first non-empty result.
        if not self._registry.enabled("cwf"):
            return None
        offices = list(self._cycle_cfg.cwf.offices) if self._cycle_cfg else []
        if not offices:
            return None
        max_chars = self._product_max_chars("CWF", ctx.mode)
        for office in offices:
            try:
                product_fetcher = getattr(self.api, "coastal_waters_forecast_product", None)
                if callable(product_fetcher):
                    product = await product_fetcher(office)
                    if not product:
                        continue
                    raw = product.product_text
                    product_id = getattr(product, "product_id", None)
                    evidence = SegmentSourceEvidence(
                        source_name="nws",
                        product_identifier=product_id,
                        product_type=getattr(product, "product_type", None) or "CWF",
                        issuing_office=getattr(product, "wfo", None) or office,
                        issuance_time=getattr(product, "issuance_time", None),
                        fetched_at=dt.datetime.now(dt.UTC),
                        source_reference=(f"https://api.weather.gov/products/{product_id}" if product_id else None),
                    )
                else:
                    raw = await self.api.coastal_waters_forecast_text(office)
                    if not raw:
                        continue
                    evidence = SegmentSourceEvidence(source_name="nws", product_type="CWF", issuing_office=office)
                # Pre-pass: strip zone routing lines (ANZ531-532-212300-)
                cleaned = _strip_marine_routing_lines(raw)
                # CWF-specific pass: boilerplate, period markers, abbrevs
                cleaned = _scrub_cwf_product_text(cleaned)
                cleaned = clean_for_tts(cleaned)
                cleaned = _scrub_nws_product_text(cleaned)
                cleaned = _trim_chars(cleaned, max_chars)
                if cleaned:
                    return cleaned, evidence
            except Exception:
                continue
        return None

    async def _acquire_rwr_source(self, ctx: CycleContext) -> tuple[RwrProduct, SegmentSourceEvidence] | None:
        rwr_cfg = self._cycle_cfg.rwr if self._cycle_cfg else None
        if not rwr_cfg or not rwr_cfg.enabled:
            return None
        try:
            fetched = await self._fetch_product("RWR", rwr_cfg.office)
            if not fetched:
                return None
            product, evidence = fetched
            parsed = parse_rwr(product.product_text, name_map=dict(rwr_cfg.station_names))
            if parsed is None:
                return None
            stale = True
            if parsed.issuance_dt:
                age_mins = (dt.datetime.now(tz=dt.timezone.utc) - parsed.issuance_dt).total_seconds() / 60
                stale = age_mins > rwr_cfg.staleness_minutes
            return None if stale else (parsed, evidence)
        except Exception:
            return None

    async def _build_asos_obs_source(self, ctx: CycleContext) -> _ObservationSourceResult | None:
        rwr_cfg = self._cycle_cfg.rwr if self._cycle_cfg else None
        intro = "And now for the current observed weather conditions in our area"
        fallback_ids = (
            list(rwr_cfg.fallback_stations) if rwr_cfg and rwr_cfg.fallback_stations else list(self.obs_stations)
        )
        if not fallback_ids:
            return None

        # Height-aware station count
        max_obs = (
            1
            if ctx.mode == "heightened"
            else ((self._cycle_cfg.obs.max_normal if self._cycle_cfg else 0) or min(8, len(fallback_ids)))
        )

        # Rotation
        sts = list(fallback_ids)
        rot_period = self._cycle_cfg.obs.rotate_period_s if self._cycle_cfg else 300
        rot_step = (self._cycle_cfg.obs.rotate_step or max_obs) if self._cycle_cfg else max_obs
        slot = int(dt.datetime.now().timestamp() // max(rot_period, 1))
        offset = (slot * max(rot_step, 1)) % len(sts)
        sts = sts[offset:] + sts[:offset]

        # Fetch ASOS observations
        name_map = dict(rwr_cfg.station_names) if rwr_cfg else {}
        anchor = sts[0] if sts else ""
        station_obs: List[Any] = []
        accepted_station: str | None = None
        accepted_properties: dict[str, Any] | None = None
        for st in sts[:max_obs]:
            try:
                props = await self.api.latest_observation(st)
                if props:
                    station_obs.append((st, props))
                    accepted_station = accepted_station or st
                    accepted_properties = accepted_properties or props
            except Exception:
                continue

        if not station_obs:
            return None

        text = build_asos_obs_text(
            stations=station_obs,
            anchor_id=anchor,
            max_compact=max_obs,
            intro_prefix=intro,
            cache=self._pressure_cache,
            name_map=name_map,
        )
        if not text or not accepted_station:
            return None
        fetched_at = dt.datetime.now(dt.UTC)
        observed_at = (accepted_properties or {}).get("timestamp")
        evidence = SegmentSourceEvidence(
            source_name="asos",
            product_type="ASOS",
            issuing_office=None,
            issuance_time=str(observed_at) if observed_at else None,
            fetched_at=fetched_at,
            source_reference=f"https://api.weather.gov/stations/{accepted_station}/observations/latest",
        )
        return _ObservationSourceResult(text=text, product=None, evidence=evidence)

    async def _build_obs_rwr_segment(
        self, ctx: CycleContext
    ) -> (
        tuple[str | None, RwrProduct | None, SegmentSourceEvidence | None]
        | tuple[str | None, RwrProduct | None]
        | _ObservationSourceResult
    ):
        """Build land observations and return the source that actually won.

        The tuple shape remains accepted for narrow legacy test doubles; the
        real path returns text, parsed RWR product, and typed source evidence.
        """
        rwr_cfg = self._cycle_cfg.rwr if self._cycle_cfg else None
        if rwr_cfg is None:
            return None, None, None
        acquired = await self._acquire_rwr_source(ctx)
        if acquired:
            product, evidence = acquired
            text = build_rwr_obs_text(
                product=product,
                anchor_names=list(rwr_cfg.anchor_stations),
                max_compact_per_section=rwr_cfg.max_compact_per_section,
                intro_prefix="And now for the current observed weather conditions in our area",
                cache=self._pressure_cache,
            )
            if text:
                return text, product, evidence
        fallback = await self._build_asos_obs_source(ctx)
        if fallback is None:
            return None, None, None
        return fallback.text, None, fallback.evidence

    async def _build_marine_obs_segment(
        self,
        ctx: CycleContext,
        rwr_product: Optional[RwrProduct],
    ) -> Optional[str]:
        """
        Build the marine observations segment from the already-parsed RWR product.
        No extra API call — marine obs are a section inside the same RWR we fetched
        for land obs.  Enabled via cycle.marine_obs.enabled = true in config.
        """
        if not self._registry.enabled("marine_obs"):
            return None
        if not rwr_product or not rwr_product.marine_stations:
            return None
        cfg = self._cycle_cfg.marine_obs
        return build_marine_obs_text(
            product=rwr_product,
            max_stations=cfg.max_stations,
            anchor_names=list(cfg.anchor_stations),
            name_map=dict(cfg.station_names),
        )

    def build_status_text(self, ctx: CycleContext) -> str:
        """Build the station-status segment from the local active-alert projection."""
        max_chars = self._cycle_cfg.last_product_max_chars if self._cycle_cfg else 260
        return build_station_status_text(
            ctx,
            ctx.active_alerts,
            last_product_max_chars=max_chars,
        )

    # ------------------------------------------------------------------
    # Independent static segment builders
    # ------------------------------------------------------------------

    async def build_health_segment(self, request: SegmentBuildInput) -> SegmentCandidate | None:
        notice = (getattr(request.context, "health_notice", None) or "").strip()
        if not notice:
            return None
        return SegmentCandidate.from_cycle_segment(
            CycleSegment(key="health", title=self._registry.title_for("health"), text=notice),
            source_name="controller-health",
            product_type="health",
        )

    async def build_hwo_segment(self, request: SegmentBuildInput) -> SegmentCandidate | None:
        fetched = await self._fetch_product("HWO", "LWX")
        if not fetched:
            if self._registry.fallback_enabled("hwo"):
                return SegmentCandidate.from_cycle_segment(
                    CycleSegment(
                        key="hwo",
                        title=self._registry.title_for("hwo"),
                        text="The hazardous weather outlook from LWX was unavailable.",
                    ),
                    source_name="nws",
                    product_type="HWO",
                    issuing_office="LWX",
                )
            return None
        product, evidence = fetched
        body = self._clean_hwo_body(product.product_text)
        body = _trim_chars(body, self._product_max_chars("HWO", request.context.mode))
        if not body:
            return None
        return SegmentCandidate.from_cycle_segment(
            CycleSegment(
                key="hwo",
                title=self._registry.title_for("hwo"),
                text="And now for the hazardous weather outlook for the service area. " + body,
            ),
            evidence=evidence,
        )

    async def build_spc_segment(self, request: SegmentBuildInput) -> SegmentCandidate | None:
        if not self._registry.enabled("spc"):
            return None
        text = await self._build_spc_outlook_text(request.context, dt.datetime.now(tz=self.tz))
        if not text:
            return None
        return SegmentCandidate.from_cycle_segment(
            CycleSegment(key="spc", title=self._registry.title_for("spc"), text=text),
            source_name="spc",
            product_type="convective_outlook",
        )

    async def build_zfp_segment(self, request: SegmentBuildInput) -> SegmentCandidate | None:
        built = await self._build_synopsis_text(request.context)
        if not built or not self._registry.enabled("zfp"):
            return None
        if isinstance(built, str):
            text, evidence = built, None
        else:
            text, evidence = built
        return SegmentCandidate.from_cycle_segment(
            CycleSegment(
                key="zfp",
                title=self._registry.title_for("zfp"),
                text=(
                    "This is the weather synopsis for our area. And now for the weather features affecting our region over the next several days. "
                    + text
                ),
            ),
            evidence=evidence,
            source_name="nws" if evidence is None else None,
        )

    def _forecast_max_points(self, ctx: CycleContext, max_periods: int) -> int:
        max_points = 1 if ctx.mode == "heightened" else (self._cycle_cfg.fc.max_points_normal if self._cycle_cfg else 6)
        if max_periods >= 10:
            return min(max_points, self._cycle_cfg.fc.max_points_7day if self._cycle_cfg else 2)
        if max_periods >= 6:
            return min(max_points, 3)
        return max_points

    def _forecast_settings(self, ctx: CycleContext) -> tuple[int, str, int, int, int, list]:
        field = "shortForecast" if (self._cycle_cfg.fc.use_short if self._cycle_cfg else True) else "detailedForecast"
        max_periods = 1 if ctx.mode == "heightened" else (self._cycle_cfg.fc.periods_normal if self._cycle_cfg else 14)
        max_points = self._forecast_max_points(ctx, max_periods)
        per_group = self._cycle_cfg.fc.periods_per_group if self._cycle_cfg else 4
        point_max = self._cycle_cfg.fc.point_max_chars if self._cycle_cfg else 1600
        return (
            max_points,
            field,
            max_periods,
            per_group,
            point_max,
            list(self._cycle_cfg.fc.forecast_zones) if self._cycle_cfg else [],
        )

    @staticmethod
    def _forecast_line(label: str, entries: list[str], per_group: int, point_max: int) -> str | None:
        if not entries:
            return None
        groups = [
            ". ".join(entries[i : i + max(1, per_group)]) + "." for i in range(0, len(entries), max(1, per_group))
        ]
        return _trim_chars(_scrub_nws_product_text(f"The forecast for {label}.\n" + "\n".join(groups)), point_max)

    async def _forecast_zone_lines(
        self, zones: list, now: dt.datetime, max_points: int, max_periods: int, per_group: int, point_max: int
    ) -> list[str]:
        rotate_period = self._cycle_cfg.fc.rotate_period_s if self._cycle_cfg else 300
        rotate_step = (self._cycle_cfg.fc.rotate_step or max_points) if self._cycle_cfg else max_points
        offset = (int(now.timestamp() // max(rotate_period, 1)) * max(rotate_step, 1)) % len(zones)
        lines: list[str] = []
        for zone_id, label in (zones[offset:] + zones[:offset])[:max_points]:
            line = await self._forecast_zone_line(zone_id, label, max_periods, per_group, point_max)
            if line:
                lines.append(line)
        return lines

    async def _forecast_zone_line(
        self, zone_id: str, label: str, max_periods: int, per_group: int, point_max: int
    ) -> str | None:
        try:
            periods = await self.api.zone_forecast_periods(zone_id)
            entries = [
                f"{_SPACE_RE.sub(' ', str(period.get('name') or '').replace('—', '-')).strip()}: "
                f"{_SPACE_RE.sub(' ', str(period.get('detailedForecast') or '').replace('—', '-')).strip()}"
                for period in periods[:max_periods]
                if str(period.get("detailedForecast") or "").strip()
            ]
            entries = [re.sub(r"\.\s*$", "", entry.strip()) for entry in entries]
            return self._forecast_line(label, entries, per_group, point_max)
        except Exception:
            return None

    async def _forecast_point_lines(
        self,
        points: list,
        now: dt.datetime,
        max_points: int,
        field: str,
        max_periods: int,
        per_group: int,
        point_max: int,
    ) -> list[str]:
        if points:
            rotate_period = self._cycle_cfg.fc.rotate_period_s if self._cycle_cfg else 300
            rotate_step = (self._cycle_cfg.fc.rotate_step or max_points) if self._cycle_cfg else max_points
            offset = (int(now.timestamp() // max(rotate_period, 1)) * max(rotate_step, 1)) % len(points)
            points = points[offset:] + points[:offset]
        lines: list[str] = []
        for lat, lon, label in points[:max_points]:
            line = await self._forecast_point_line(lat, lon, label, field, max_periods, per_group, point_max)
            if line:
                lines.append(line)
        return lines

    @staticmethod
    def _forecast_point_entries(periods: list[dict], field: str, max_periods: int) -> list[str]:
        entries = []
        for period in periods[:max_periods]:
            name = _SPACE_RE.sub(" ", str(period.get("name") or "").replace("—", "-")).strip()
            value = _SPACE_RE.sub(" ", str(period.get(field) or "").replace("—", "-")).strip()
            if not value:
                continue
            temperature = period.get("temperature")
            if isinstance(temperature, (int, float)):
                phrase = f" With a {'high near' if bool(period.get('isDaytime', True)) else 'low around'} {round(temperature)} degrees"
                value += phrase
            entries.append(f"{name}: {value}" if name else value)
        return entries

    async def _forecast_point_line(
        self, lat: float, lon: float, label: str, field: str, max_periods: int, per_group: int, point_max: int
    ) -> str | None:
        try:
            periods = await self.api.point_forecast_periods(lat, lon)
            entries = self._forecast_point_entries(periods, field, max_periods)
            return self._forecast_line(label, entries, per_group, point_max)
        except Exception:
            return None

    async def build_fcst_segment(self, request: SegmentBuildInput) -> SegmentCandidate | None:
        ctx = request.context
        max_points, field, max_periods, per_group, point_max, zones = self._forecast_settings(ctx)
        now = dt.datetime.now(tz=self.tz)
        fc_lines = (
            await self._forecast_zone_lines(zones, now, max_points, max_periods, per_group, point_max)
            if zones
            else await self._forecast_point_lines(
                list(self.points), now, max_points, field, max_periods, per_group, point_max
            )
        )
        if not fc_lines or not self._registry.enabled("fcst"):
            return None
        prefix = (
            "This is the summarized forecast section for our area. "
            if ctx.mode == "heightened"
            else "This is the overall forecast section for our area from the National Weather Service. "
        )
        return SegmentCandidate.from_cycle_segment(
            CycleSegment(key="fcst", title=self._registry.title_for("fcst"), text=prefix + " ".join(fc_lines)),
            source_name="nws",
            product_type="forecast",
        )

    async def build_cwf_segment(self, request: SegmentBuildInput) -> SegmentCandidate | None:
        built = await self._build_cwf_text_with_evidence(request.context)
        if not built or not self._registry.enabled("cwf"):
            return None
        text, evidence = built
        return SegmentCandidate.from_cycle_segment(
            CycleSegment(
                key="cwf",
                title=self._registry.title_for("cwf"),
                text="And now for the coastal and marine weather forecast for our area. " + text,
            ),
            evidence=evidence,
        )

    async def build_obs_segment(self, request: SegmentBuildInput) -> SegmentCandidate | None:
        built = await self._build_obs_rwr_segment(request.context)
        text: str | None
        evidence: SegmentSourceEvidence | None
        if isinstance(built, _ObservationSourceResult):
            text, evidence = built.text, built.evidence
        elif len(built) >= 3:
            text, _product, evidence = built
        else:
            text, evidence = built[0], None
        if not text or not self._registry.enabled("obs"):
            return None
        return SegmentCandidate.from_cycle_segment(
            CycleSegment(key="obs", title=self._registry.title_for("obs"), text=text),
            evidence=evidence,
            source_name="nws" if evidence is None else None,
            product_type="RWR" if evidence is None else None,
        )

    async def build_marine_obs_segment(self, request: SegmentBuildInput) -> SegmentCandidate | None:
        acquired = await self._acquire_rwr_source(request.context)
        product = acquired[0] if acquired else None
        evidence = acquired[1] if acquired else None
        text = await self._build_marine_obs_segment(request.context, product)
        if not text or not self._registry.enabled("marine_obs"):
            return None
        return SegmentCandidate.from_cycle_segment(
            CycleSegment(
                key="marine_obs",
                title=self._registry.title_for("marine_obs"),
                text="And now for the marine observations in the service area. " + text,
            ),
            evidence=evidence,
            source_name="nws" if evidence is None else None,
            product_type="RWR" if evidence is None else None,
        )

    async def build_outro_segment(self, request: SegmentBuildInput) -> SegmentCandidate:
        return SegmentCandidate.from_cycle_segment(
            CycleSegment(
                key="outro",
                title=self._registry.title_for("outro"),
                text="This is the end of the current broadcast cycle. Updated information will follow on the next rotation.",
            ),
            source_name="controller",
            product_type="outro",
        )

    async def build_segments(
        self,
        station_name: str,
        service_area_name: str,
        disclaimer: str,
        ctx: CycleContext,
    ) -> List[CycleSegment]:
        """Compatibility composer over the canonical independent builders."""
        request = SegmentBuildInput(
            key="",
            context=ctx,
            station_name=station_name,
            service_area_name=service_area_name,
            disclaimer=disclaimer,
        )
        if getattr(ctx, "health_detached_loop_only", False):
            notice = (
                getattr(ctx, "health_notice", None)
                or "SeasonalWeather is temporarily unable to receive current National Weather Service information. Please use another weather information source or visit weather.gov for the latest information."
            ).strip()
            return [
                CycleSegment(key="id", title=self._registry.title_for("id"), text=notice),
                CycleSegment(key="health", title=self._registry.title_for("health"), text=notice),
            ]

        station_id = station_id_text(ctx, station_name, service_area_name, disclaimer)

        segments: list[CycleSegment] = [
            CycleSegment(key="id", title=self._registry.title_for("id"), text=station_id),
        ]
        health = await self.build_health_segment(request)
        if health is not None:
            segments.append(CycleSegment(health.key, health.title, health.text))
        segments.append(
            CycleSegment(
                key="status",
                title=self._registry.title_for("status"),
                text=self.build_status_text(ctx),
            )
        )

        builders = (
            self.build_hwo_segment,
            self.build_spc_segment,
            self.build_zfp_segment,
            self.build_fcst_segment,
            self.build_cwf_segment,
            self.build_obs_segment,
            self.build_marine_obs_segment,
            self.build_outro_segment,
        )
        for builder in builders:
            candidate = await builder(request)
            if candidate is not None:
                segments.append(CycleSegment(candidate.key, candidate.title, candidate.text))
        return segments

    def _hwo_unavailable_segment(self) -> CycleSegment | None:
        if not self._registry.enabled("hwo") or not self._registry.fallback_enabled("hwo"):
            return None
        return CycleSegment(
            key="hwo-unavailable",
            title=self._registry.title_for("hwo"),
            text="The hazardous weather outlook from LWX was unavailable.",
        )

    async def build_text(
        self,
        station_name: str,
        service_area_name: str,
        disclaimer: str,
        ctx: CycleContext,
    ) -> str:
        segs = await self.build_segments(station_name, service_area_name, disclaimer, ctx)
        return "\n\n".join(s.text for s in segs if s.text and s.text.strip())
