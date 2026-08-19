"""Parsing and rendering for the Mid-Atlantic OFFNT2 forecast.

The parser is deliberately independent of the cycle registry and controller
state.  It accepts only the Ocean Prediction Center's Mid-Atlantic product
identity and returns bounded zone/synopsis data for the registry-owned cycle
builder to render.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass
from math import floor

_EXPECTED_AWIPS = "OFFNT2"
_EXPECTED_WMO = "FZNT22 KWBC"
_WMO_RE = re.compile(r"\bFZNT\d{2}\s+[A-Z]{4}\b", re.IGNORECASE)
_AWIPS_LINE_RE = re.compile(r"^\s*(OFFNT\d+)\s*$", re.IGNORECASE)
_SYNOPSIS_RE = re.compile(r"^\s*\.?SYNOPSIS(?:\b|\.{3})", re.IGNORECASE)
_SYNOPSIS_PREFIX_RE = re.compile(r"^\s*\.?SYNOPSIS(?:\s+FOR\b[^.]*?WATERS)?\.{0,3}", re.IGNORECASE)
_PERIOD_RE = re.compile(
    r"^\s*(REST OF TONIGHT|REST OF TODAY|TONIGHT|TODAY|SUN NIGHT|MON NIGHT|TUE NIGHT|WED NIGHT|"
    r"THU NIGHT|FRI NIGHT|SAT NIGHT|SUN|MON|TUE|WED|THU|FRI|SAT)\s*\.{3}(.*)$",
    re.IGNORECASE,
)
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
_ISSUANCE_RE = re.compile(
    r"^\s*\d{3,4}\s+(?:AM|PM)\s+[A-Z]{3}\s+\w{3}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{4}\s*$",
    re.IGNORECASE,
)
_WARNING_RE = re.compile(
    r"\b(?:HURRICANE\s+FORCE\s+WIND|STORM|GALE|TROPICAL\s+STORM|HURRICANE|"
    r"SMALL\s+CRAFT|DENSE\s+FOG)\s+(?:WARNING|WATCH|ADVISORY)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Offnt2ZoneForecast:
    """One routed OFFNT2 forecast block and the zones it covers."""

    zone_ids: tuple[str, ...]
    text: str
    warning_headlines: tuple[str, ...] = ()


@dataclass(frozen=True)
class Offnt2Product:
    """Validated OFFNT2 product content."""

    awips_id: str
    wmo_heading: str
    synopsis: str | None
    zones: tuple[Offnt2ZoneForecast, ...]


def _clean_line(value: str) -> str:
    value = value.replace("\r", "").replace("—", "-")
    value = re.sub(r"\s+", " ", value).strip(" \t").lstrip(".").strip()
    return value


def _is_routing_line(value: str) -> bool:
    parts = [part for part in value.strip().split("-") if part]
    if len(parts) < 2 or not re.fullmatch(r"ANZ\d{3}", parts[0], re.IGNORECASE):
        return False
    for part in parts[1:]:
        if re.fullmatch(r"\d{3}", part) or re.fullmatch(r"\d{6}", part):
            continue
        return False
    return bool(re.fullmatch(r"\d{6}", parts[-1]))


def _routing_zones(value: str) -> tuple[str, ...] | None:
    if not _is_routing_line(value):
        return None
    parts = [part for part in value.strip().split("-") if part]
    prefix = parts[0][:3].upper()
    zones = [parts[0].upper()]
    zones.extend(f"{prefix}{part}" for part in parts[1:-1])
    return tuple(zones)


def _is_issuance_line(value: str) -> bool:
    return bool(_ISSUANCE_RE.fullmatch(value))


def _spoken_period_line(value: str) -> str:
    match = _PERIOD_RE.match(value)
    if not match:
        return value
    period = _PERIOD_MAP[match.group(1).upper()]
    remainder = match.group(2).strip()
    return f"{period}. {remainder}" if remainder else f"{period}."


def _issuance_preamble_indices(lines: Sequence[str]) -> set[int]:
    skipped: set[int] = set()
    for index, line in enumerate(lines):
        if not _is_issuance_line(_clean_line(line)):
            continue
        skipped.add(index)
        for previous in range(index - 1, -1, -1):
            if not _clean_line(lines[previous]):
                break
            skipped.add(previous)
    return skipped


def _clean_section(lines: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    cleaned: list[str] = []
    warnings: list[str] = []
    raw_lines = list(lines)
    skipped = _issuance_preamble_indices(raw_lines)
    for index, raw in enumerate(raw_lines):
        line = _clean_line(raw)
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


def _routing_sections(lines: Sequence[str]) -> list[tuple[int, tuple[str, ...]]]:
    routing: list[tuple[int, tuple[str, ...]]] = []
    for index, line in enumerate(lines):
        routing_zones = _routing_zones(line)
        if routing_zones:
            routing.append((index, routing_zones))
    return routing


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


def _canonical(value: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (value or "").lower()).strip()


def _same_synopsis(left: str | None, right: str | None) -> bool:
    a, b = _canonical(left), _canonical(right)
    return bool(a and b and (a == b or a in b or b in a))


def _word_count(value: str) -> int:
    return len(value.split())


def _within_budget(value: str, max_chars: int, max_words: int) -> bool:
    return (not max_chars or len(value) <= max_chars) and (not max_words or _word_count(value) <= max_words)


def _append_optional(base: str, addition: str, max_chars: int, max_words: int) -> str:
    candidate = f"{base} {addition}".strip()
    if max_chars and len(candidate) > max_chars:
        return base
    if max_words and _word_count(candidate) > max_words:
        return base
    return candidate


def _zone_parts(label: str, block: Offnt2ZoneForecast) -> tuple[str, str]:
    warning_text = " ".join(block.warning_headlines)
    protected = f"The forecast for {label}."
    if warning_text:
        protected = f"{protected} {warning_text}."
    remaining = block.text
    for warning in block.warning_headlines:
        remaining = re.sub(re.escape(warning), "", remaining, flags=re.IGNORECASE)
    return protected, re.sub(r"\s+", " ", remaining).strip(" .")


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


def _configured_zones(configured_zones: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    configured = [(str(zone).upper().strip(), str(label).strip()) for zone, label in configured_zones]
    return [(zone, label or zone) for zone, label in configured if re.fullmatch(r"ANZ\d{3}", zone)]


def _rotated_zones(
    configured: list[tuple[str, str]], rotate_period_s: int, rotate_step: int, now: dt.datetime
) -> list[tuple[str, str]]:
    if not configured:
        return []
    period = max(1, rotate_period_s)
    offset = (int(now.timestamp() // period) * (rotate_step or 1)) % len(configured)
    return configured[offset:] + configured[:offset]


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


def _prioritized_zones(
    selected: list[tuple[str, Offnt2ZoneForecast]], heightened: bool, defer_in_heightened: bool
) -> list[tuple[str, Offnt2ZoneForecast]] | None:
    warning_selected = [(label, block) for label, block in selected if block.warning_headlines]
    if heightened and defer_in_heightened:
        return warning_selected or None
    return warning_selected + [item for item in selected if not item[1].warning_headlines]


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


__all__ = [
    "Offnt2Product",
    "Offnt2ZoneForecast",
    "parse_offnt2_product",
    "render_offnt2",
]
