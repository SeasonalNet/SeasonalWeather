"""Pure common preprocessing for every synthesis backend."""

from __future__ import annotations


import time

from threading import Event


from .models import MAX_SYNTHESIS_TEXT, TextOverride
from .regex_safety import (
    MAX_CONFIGURED_REGEX_PATTERN,
    MAX_CONFIGURED_REGEX_REPLACEMENT,
    MAX_CONFIGURED_REGEX_REPLACEMENTS,
    MAX_CONFIGURED_REGEX_RULES,
    compile_safe_regex,
    validate_replacement,
)
from .cancellation import deadline_expired, explicit_cancellation

from .subprocess import ProcessFailure

import re


PREPROCESSING_VERSION = "tts-preprocess-v1"
MAX_OVERRIDE_PATTERN = MAX_CONFIGURED_REGEX_PATTERN
MAX_OVERRIDE_REPLACEMENTS = MAX_CONFIGURED_REGEX_REPLACEMENTS
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
        return compile_safe_regex(match, flags=flags)
    return compile_safe_regex(re.escape(match), flags=flags)


def _apply_text_overrides(text: str, overrides: list[object] | None) -> str:
    s = text or ""
    if len(overrides or []) > MAX_CONFIGURED_REGEX_RULES:
        raise ValueError("too many text overrides")
    for spec in overrides or []:
        if not isinstance(spec, dict):
            raise ValueError("text override must be an object")
        repl = str(spec.get("replace", "") or "")
        match = str(spec.get("match", "") or "")
        if not match:
            raise ValueError("text override is missing 'match'")
        if len(match) > MAX_OVERRIDE_PATTERN or len(repl) > MAX_CONFIGURED_REGEX_REPLACEMENT:
            raise ValueError("text override pattern or replacement is overlong")
        validate_replacement(repl)
        rx = _compile_text_override_rx(spec)
        s, count = rx.subn(repl, s, count=MAX_OVERRIDE_REPLACEMENTS + 1)
        if count > MAX_OVERRIDE_REPLACEMENTS:
            raise ValueError("text override replacement work exceeded its bound")
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


def _check_fence(deadline: float | None, cancellation: Event | None, stage: str) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise ProcessFailure("timed_out", f"synthesis deadline expired during {stage}")
    if deadline_expired(cancellation):
        raise ProcessFailure("timed_out", f"synthesis deadline expired during {stage}")
    if explicit_cancellation(cancellation):
        raise ProcessFailure("cancelled", f"synthesis was cancelled during {stage}")


def _compile_bounded_override(override: TextOverride) -> re.Pattern[str]:
    if len(override.match) > MAX_OVERRIDE_PATTERN:
        raise ValueError("text override pattern is overlong")
    expression = compile_safe_regex(
        re.escape(override.match) if not override.regex else override.match,
        flags=re.IGNORECASE if override.ignore_case else 0,
    )
    validate_replacement(override.replace)
    return expression


def preprocess_text(
    text: str,
    overrides: tuple[TextOverride, ...] = (),
    *,
    deadline: float | None = None,
    cancellation: Event | None = None,
) -> str:
    if len(overrides) > MAX_CONFIGURED_REGEX_RULES:
        raise ValueError("too many text overrides")
    _check_fence(deadline, cancellation, "preprocessing")
    result = clean_for_tts(text)
    _check_fence(deadline, cancellation, "preprocessing")
    result = normalize_nws_spoken_times(result)
    for override in overrides:
        _check_fence(deadline, cancellation, "text overrides")
        if len(override.replace) > MAX_CONFIGURED_REGEX_REPLACEMENT:
            raise ValueError("text override replacement is overlong")
        expression = _compile_bounded_override(override)
        result, count = expression.subn(override.replace, result, count=MAX_OVERRIDE_REPLACEMENTS + 1)
        if count > MAX_OVERRIDE_REPLACEMENTS:
            raise ValueError("text override replacement work exceeded its bound")
        if len(result) > MAX_SYNTHESIS_TEXT:
            raise ValueError("preprocessed synthesis text is overlong")
    result = result.strip()
    if not result:
        raise ValueError("preprocessing produced empty synthesis text")
    _check_fence(deadline, cancellation, "preprocessing")
    return result
