"""
product_text.py — NWS product text helpers and alert script builders.

This module owns:
  - Pure text utilities shared across the alert pipeline (clean_cap_text,
    join_oxford, parse_cap_area_by_state, etc.)
  - CAP-path script builders (build_statement_vtec_action_script,
    build_warning_vtec_action_script)
  - NWWS-path helpers (expiry_summary_script, cap_prefers_statement_update_script,
    build_nwws_partial_cancel_script, parse_nwws_product_segments)
  - Existing helpers (cap_statement_intro, cap_statement_area_noun, etc.)

Design rules:
  - No imports from seasonalweather.main.  Pure stdlib + intra-package only.
  - No side effects.  All functions are pure / stateless unless noted.
  - Orchestrator methods that previously did this work become thin shims that
    call these free functions, forwarding self._tz, self._cap_vtec_list(ev)
    etc. as explicit arguments.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Marine detection constants
# ---------------------------------------------------------------------------
_MARINE_UGC_RE = re.compile(r"\b(?:ANZ|AMZ|GMZ|LMZ|PHZ|PKZ|PZZ|SLZ)\d{3}\b", re.IGNORECASE)
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
_MARINE_PHEN = {"SC", "GL", "SR", "HF", "SE", "UP", "RB", "SI", "BW", "MF", "MH", "MS", "LO", "SU", "MA"}


# ---------------------------------------------------------------------------
# US state abbreviation -> full name
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# NWS header / spoken time helpers
# ---------------------------------------------------------------------------
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

_NWS_HEADER_ISSUED_RE = re.compile(
    r"^(?P<hhmm>\d{3,4})\s*(?P<ampm>AM|PM)\s*(?P<tz>[A-Z]{2,4})\s+"
    r"(?P<dow>[A-Za-z]{3})\s+(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})\s*$"
)

_SPS_INTRO_LEAD_RE = re.compile(
    r"(?is)^(?:This is a statement from the National Weather Service\.|The National Weather Service has issued the following message\.)\s*"
)


def expand_tz_token(token: str) -> str:
    tok = (token or "").strip()
    if not tok:
        return "local"
    return _TZ_NAME_MAP.get(tok.upper(), tok)


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


def sps_preamble(sent_iso: str | None = None, *, local_tz: dt.tzinfo | None = None) -> str:
    """
    Build the NWR-style Special Weather Statement preamble used by CAP and NWWS paths.
    """
    issued = fmt_local_from_utc_iso(sent_iso or "", local_tz=local_tz)
    if issued:
        return f"And now a Special Weather Statement from your National Weather Service, issued at {issued}."
    return "And now a Special Weather Statement from your National Weather Service."


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


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------


def clean_cap_text(s: str, *, limit: int = 900) -> str:
    """Normalise whitespace, collapse ellipses, strip stray AWIPS IDs."""
    s2 = (s or "").replace("\r", " ").replace("\n", " ")
    s2 = re.sub(r"\s+", " ", s2).strip()
    s2 = s2.replace("...", ". ").replace("..", ".")
    s2 = re.sub(r"^[A-Z][A-Z0-9]{4,7}\s+", "", s2)
    if len(s2) > limit:
        s2 = s2[:limit].rstrip() + "..."
    return s2


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


# ---------------------------------------------------------------------------
# CAP statement / area helpers (pre-existing, preserved)
# ---------------------------------------------------------------------------


def cap_is_special_weather_statement(event: str | None) -> bool:
    return str(event or "").strip().lower() == "special weather statement"


def cap_normalize_nws_headline(parameters: dict[str, list[str]] | None) -> str:
    params = parameters or {}
    nws_hl_list = params.get("NWSheadline") or []
    nws_hl = str(nws_hl_list[0]).strip() if nws_hl_list else ""
    if nws_hl and nws_hl.isupper():
        nws_hl = nws_hl.capitalize()
    return nws_hl


cap_nwsheadline = cap_normalize_nws_headline  # back-compat alias


def _iter_param_values(parameters: dict[str, list[str]] | None, key: str) -> list[str]:
    params = parameters or {}
    vals = params.get(key) or []
    return [str(v).strip() for v in vals if str(v).strip()]


def _same_codes_from_event(ev: Any) -> list[str]:
    vals = getattr(ev, "same_fips", None) or []
    out: list[str] = []
    for v in vals:
        s = re.sub(r"\D+", "", str(v))
        if s:
            out.append(s.zfill(6))
    return out


def _same_codes_from_parameters(parameters: dict[str, list[str]] | None) -> list[str]:
    out: list[str] = []
    for raw in _iter_param_values(parameters, "SAME") + _iter_param_values(parameters, "FIPS6"):
        for part in re.split(r"[\s,;]+", raw):
            s = re.sub(r"\D+", "", part)
            if s:
                out.append(s.zfill(6))
    return out


def _has_marine_ugc(parameters: dict[str, list[str]] | None) -> bool:
    for raw in _iter_param_values(parameters, "UGC"):
        if _MARINE_UGC_RE.search(raw):
            return True
    return False


def _has_marine_vtec(vtec: list[str] | None) -> bool:
    for raw in vtec or []:
        m = re.search(r"/O\.[A-Z]+\.[A-Z]{4}\.([A-Z]{2})\.[A-Z]\.", str(raw).upper())
        if m and m.group(1) in _MARINE_PHEN:
            return True
    return False


def _looks_marine_text(area_desc: str | None) -> bool:
    text = str(area_desc or "").strip().lower()
    return bool(text) and any(hint in text for hint in _MARINE_AREA_HINTS)


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


def cap_area_label(ev: Any) -> str:
    """Back-compat alias."""
    return cap_statement_area_noun(
        event=getattr(ev, "event", None),
        area_desc=getattr(ev, "area_desc", None),
        parameters=getattr(ev, "parameters", {}) or {},
        vtec=getattr(ev, "vtec", None) or [],
        ev=ev,
    )


def cap_statement_intro(
    *,
    event: str | None,
    sent_iso: str | None,
    sps_preamble: Callable[[str | None], str],
) -> str:
    if cap_is_special_weather_statement(event):
        return sps_preamble(sent_iso)
    return "This is a statement from the National Weather Service."


def cap_uses_sps_preamble(ev: Any, event: str | None) -> bool:
    """Back-compat alias."""
    return cap_is_special_weather_statement(event)


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


# ---------------------------------------------------------------------------
# Expiry / cancellation helpers
# ---------------------------------------------------------------------------

_EXPIRY_SUMMARY_TZ_RE = re.compile(
    r"\b(EDT|EST|CDT|CST|MDT|MST|PDT|PST|AKDT|AKST|HST)\b",
    re.IGNORECASE,
)
_EXPIRY_SUMMARY_AMPM_RE = re.compile(r"\b(AM|PM)\b", re.IGNORECASE)


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


# ---------------------------------------------------------------------------
# CAP script builders (free functions — Orchestrator shims call these)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# NWWS multi-segment partial cancel machinery
# ---------------------------------------------------------------------------

_SEG_VTEC_RE = re.compile(
    r"/[A-Z]\.(?P<act>[A-Z]{3})\.[A-Z]{4}\.[A-Z0-9]{2}\.[A-Z]\.\d{4}\.",
    re.IGNORECASE,
)
_SEG_HEADLINE_RE = re.compile(r"^\.\.\.(.+?)\.\.\.$")
_SEG_UNTIL_RE = re.compile(r"\bUNTIL\s+([\d:]+\s*(?:AM|PM)\s+[A-Z]{2,4})", re.IGNORECASE)
_SEG_AREA_INTRO_RE = re.compile(
    r"^(?:The\s+affected\s+areas?\s*(?:were|are|include[sd]?)?|"
    r"For\s+the\s+following\s+areas?)[.\s]*",
    re.IGNORECASE,
)
_SEG_META_RE = re.compile(
    r"^(?:LAT\.\.\.LON|TIME\.\.\.MOT\.\.\.LOC|HAIL\.\.\.|WIND\.\.\.|NNNN)",
    re.IGNORECASE,
)
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
_SEG_PPA_RE = re.compile(r"^PRECAUTIONARY/PREPAREDNESS ACTIONS", re.IGNORECASE)
_SEG_LOC_RE = re.compile(r"^Locations?\s+(?:impacted|affected)\s+include", re.IGNORECASE)
_SEG_ACTION_LABEL_RE = re.compile(r"^(?:CANCELLED|CONTINUED|EXPIRED)(?:\.{1,3})?$", re.IGNORECASE)
_SEG_SCOPE_HEADER_RE = re.compile(
    r"^FOR\s+[A-Z0-9 ,/\-]+(?:COUNTY|COUNTIES|PARISH|PARISHES|CITY|CITIES|BOROUGH|BOROUGHS)\.?\.?.*$"
)
_SEG_UGC_RE = re.compile(
    r"^(?:[A-Z]{2}[CZ]\d{3}|\d{3})(?:-(?:[A-Z]{2}[CZ]\d{3}|\d{3}))*-\d{6}-?$",
    re.IGNORECASE,
)
_SEG_TIMESTAMP_RE = re.compile(r"^\d{3,4}\s+(?:AM|PM)\s+[A-Z]{2,4}")
_TZ_FIX_RE = re.compile(r"\b(EDT|EST|CDT|CST|MDT|MST|PDT|PST|AKDT|AKST|HST)\b", re.IGNORECASE)
_AMPM_FIX_RE = re.compile(r"\b(AM|PM)\b", re.IGNORECASE)


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


@dataclass
class NwwsProductSegment:
    """One $$-delimited section of a multi-segment NWWS product."""

    actions: set[str]  # VTEC action codes present (e.g. {"CAN"}, {"CON"})
    headline: str  # Cleaned headline from ...X... line
    area_text: str  # Pipe-joined geographic area names
    reason_text: str  # Why the event is occurring/ending (narrative prose)
    precautions: str  # PRECAUTIONARY/PREPAREDNESS content
    expiry_phrase: str  # e.g. "315 PM EDT" extracted from headline


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


def _clean_county_area_text(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip()).strip("-")
    text = re.sub(r"-\s*", "; ", text).strip(" ;")
    return text


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


def _ensure_sentence(text: str) -> str:
    s = (text or "").strip()
    if s and not s.endswith((".", "!", "?")):
        s += "."
    return s


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


# ---------------------------------------------------------------------------
# NWWS WCN watch script helpers
# ---------------------------------------------------------------------------
_WATCH_VTEC_RE = re.compile(
    r"/O\.(?P<action>[A-Z]{3})\.(?P<office>[A-Z]{4})\."
    r"(?P<phen>[A-Z]{2})\.(?P<sig>[A-Z])\."
    r"(?P<etn>\d{4})\."
    r"(?P<start>\d{6,8}T\d{4}Z)-(?P<end>\d{6,8}T\d{4}Z)/"
)

_STATE_ABBR_BY_FULL = {v.upper(): k for k, v in STATE_NAME_FULL.items()}
_STATE_ABBR_BY_FULL["DISTRICT OF COLUMBIA"] = "DC"
_STATE_ABBR_BY_FULL["THE DISTRICT OF COLUMBIA"] = "DC"

_WCN_AREA_STOP_RE = re.compile(
    r"^(?:THIS INCLUDES THE CITIES|PRECAUTIONARY/PREPAREDNESS|&&|\$\$|LAT\.\.\.LON|TIME\.\.\.MOT\.\.\.LOC|NNNN\b)",
    re.IGNORECASE,
)
_WCN_STATE_COUNT_RE = re.compile(
    r"^IN (?P<state>[A-Z ]+?) "
    r"(?:(?:THIS )?(?:WATCH INCLUDES|CANCELS|ALLOWS TO EXPIRE|ALLOWED TO EXPIRE)|THE NEW WATCH INCLUDES) \d+ "
    r"(?P<kind>COUNTY|COUNTIES|CITY|CITIES|INDEPENDENT CITIES)\b",
    re.IGNORECASE,
)


def _watch_vtec_match(raw: object, wanted_action: str) -> re.Match[str] | None:
    match = _WATCH_VTEC_RE.search(str(raw).strip().upper())
    if match is None or (wanted_action and match.group("action") != wanted_action):
        return None
    if match.group("sig") != "A" or match.group("phen") not in {"TO", "SV"}:
        return None
    return match


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


def _watch_area_group_phrases(groups: dict[str, list[str]], order: list[str]) -> list[str]:
    segs: list[str] = []
    for st in order:
        st_full = STATE_NAME_FULL.get(st, st)
        county_list = join_oxford(groups.get(st, []))
        if county_list:
            segs.append(f"in {st_full}: {county_list}")
    return segs


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


def extract_nwws_wcn_area_desc(text: str) -> str:
    """Public wrapper for extracting CAP-like areaDesc from an NWWS WCN product."""
    return _extract_wcn_area_desc(text)


def _wcn_area_match_key(name: str, state: str = "") -> str:
    """Normalise county/city + state labels for WCN areaDesc ↔ SAME-label matching."""
    n = str(name or "").strip().replace("’", "'")
    st = str(state or "").strip().upper()
    # NWS zone names sometimes include suffixes while WCN text usually omits them.
    n = re.sub(r"\b(?:COUNTY|CITY|PARISH|BOROUGH|MUNICIPALITY)\b", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\bTHE\b", " ", n, flags=re.IGNORECASE)
    blob = f"{n} {st}"
    return re.sub(r"[^a-z0-9]+", "", blob.lower())


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


def _watch_label_and_remember(kind: str, watch_number: int | None) -> tuple[str, str, str]:
    """Return (watch_label, numbered_label, remember_text) for TO.A/SV.A watches."""
    if kind == "tornado":
        watch_label = "Tornado Watch"
        remember = (
            "Remember, a tornado watch means that conditions are favorable for the development of severe weather, "
            "including tornadoes, large hail, and damaging winds, in and close to the watch area. "
            "While severe weather may not be imminent, persons should remain alert for rapidly changing weather conditions, "
            "and listen for later statements and possible warnings."
        )
    else:
        watch_label = "Severe Thunderstorm Watch"
        remember = (
            "Remember, a severe thunderstorm watch means that conditions are favorable for the development of severe weather, "
            "including large hail and damaging winds, in and close to the watch area. "
            "While severe weather may not be imminent, persons should remain alert for rapidly changing weather conditions, "
            "and listen for later statements and possible warnings."
        )
    label_with_num = f"{watch_label} Number {watch_number}" if watch_number is not None else watch_label
    return watch_label, label_with_num, remember


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


@dataclass(frozen=True)
class NwwsScriptRenderResult:
    script: str
    changed: bool = False
    renderer: str = "base"
    notes: tuple[str, ...] = ()


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


def render_nwws_product_script(**kwargs) -> NwwsScriptRenderResult:
    """Compatibility wrapper for the central NWS product formatter."""
    return render_nws_product_script(**kwargs)


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
