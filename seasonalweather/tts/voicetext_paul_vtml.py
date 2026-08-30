from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Callable, Pattern, cast

from .regex_safety import (
    MAX_CONFIGURED_REGEX_PATTERN,
    MAX_CONFIGURED_REGEX_REPLACEMENT,
    MAX_CONFIGURED_REGEX_REPLACEMENTS,
    MAX_CONFIGURED_REGEX_RULES,
    compile_safe_regex,
    validate_replacement,
)

# Split text vs tags so we never “rewrite inside markup”.
_TAG_SPLIT_RE = re.compile(r"(<[^>]+>)")
_VTML_BLOCK_RE = re.compile(
    r"<(?P<tag>vtml_[A-Za-z0-9_]+)\b[^>]*>.*?</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_AWIPS_PHONEME_RE = re.compile(
    r'<phoneme\b[^>]*\balphabet="x-cmu"[^>]*\bph="([^"]*)"[^>]*>\s*</phoneme>',
    re.IGNORECASE,
)
_AWIPS_BREAK_RE = re.compile(r'<break\b[^>]*\bstrength="([^"]+)"[^>]*/>', re.IGNORECASE)

# _env_enabled() removed — vtml_lexicon is now passed as a function argument.


@dataclass(frozen=True)
class Rule:
    rx: Pattern[str]
    repl: Callable[[re.Match[str]], str]
    user_configured: bool = False


def _sub_alias(alias: str) -> Callable[[re.Match[str]], str]:
    # Keep the original surface form (case/punct) inside the tag.
    escaped_alias = html.escape(alias, quote=True)

    def _r(m: re.Match[str]) -> str:
        return f'<vtml_sub alias="{escaped_alias}">{m.group(0)}</vtml_sub>'

    return _r


def _word_with_trailing_pause(ms: int) -> Callable[[re.Match[str]], str]:
    """Return the matched word as-is, followed by a VTML pause of <ms> milliseconds.
    Used to insert a hard word-boundary hint so Paul doesn't garble the following token.
    ms=0 is the conventional NWR trick for words like 'fog' that otherwise bleed."""

    def _r(m: re.Match[str]) -> str:
        return f'{m.group(0)}<vtml_pause time="{ms}"/>'

    return _r


def _phoneme_x_cmu(ph: str) -> Callable[[re.Match[str]], str]:
    def _r(m: re.Match[str]) -> str:
        # x-cmu uses CMU dict phonemes with stress numbers (ASCII-friendly).
        return f'<vtml_phoneme alphabet="x-cmu" ph="{ph}">{m.group(0)}</vtml_phoneme>'

    return _r


# Direction abbreviations -> full words, but ONLY when used as wind direction.
_DIR_MAP = {
    "N": "north",
    "S": "south",
    "E": "east",
    "W": "west",
    "NE": "northeast",
    "NW": "northwest",
    "SE": "southeast",
    "SW": "southwest",
    "NNE": "north-northeast",
    "ENE": "east-northeast",
    "ESE": "east-southeast",
    "SSE": "south-southeast",
    "SSW": "south-southwest",
    "WSW": "west-southwest",
    "WNW": "west-northwest",
    "NNW": "north-northwest",
}


def _wind_dir_repl(m: re.Match[str]) -> str:
    tok = m.group("dir")
    alias = _DIR_MAP.get(tok.upper())
    if not alias:
        return tok
    return f'<vtml_sub alias="{alias}">{tok}</vtml_sub>'


def _wind_range_end_repl(m: re.Match[str]) -> str:
    tok = m.group("dir")
    alias = _DIR_MAP.get(tok.upper())
    if not alias:
        return m.group(0)
    return f'{m.group("prefix")}<vtml_sub alias="{alias}">{tok}</vtml_sub>'


# State/territory abbreviations -> spoken names, but only in place-name contexts
# like "Baltimore MD", "Smith Point VA", "Washington DC", etc.
_STATE_MAP = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
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
    "PR": "Puerto Rico",
    "VI": "U.S. Virgin Islands",
    "GU": "Guam",
    "AS": "American Samoa",
    "MP": "Northern Mariana Islands",
}

_AMBIGUOUS_STATE_CODES = {"IN", "OR", "ME", "HI", "OK"}

# If one of the ambiguous state codes appears after one of these words,
# it's probably normal sentence text, not a place name.
_PLACE_STOPWORDS = {
    "A",
    "AN",
    "AND",
    "ARE",
    "AS",
    "AT",
    "BE",
    "BECOME",
    "BECOMING",
    "BEEN",
    "BEING",
    "BY",
    "CAN",
    "COULD",
    "EXPECTED",
    "FOR",
    "FROM",
    "HAS",
    "HAVE",
    "IN",
    "INTO",
    "IS",
    "IT",
    "ITS",
    "LIKELY",
    "MAY",
    "MOVING",
    "NEAR",
    "OF",
    "ON",
    "OR",
    "POSSIBLE",
    "REMAIN",
    "REMAINS",
    "REMAINING",
    "SHOULD",
    "THE",
    "THAT",
    "THESE",
    "THIS",
    "THOSE",
    "TO",
    "WAS",
    "WERE",
    "WILL",
    "WITH",
}

_PLACE_STATE_RE = re.compile(
    r"(?P<prefix>\b(?:[A-Z][A-Za-z.'’\-]*)(?:[ ,]+[A-Z][A-Za-z.'’\-]*){0,5}[ ,]+)"
    r"(?P<st>" + "|".join(sorted(_STATE_MAP, key=len, reverse=True)) + r")"
    r"(?=(?:\s+(?:to|and)\b)|(?:\s*(?:/|,|\.{1,3}|;|:|\)|$)))"
)

_PLACE_WORD_RE = re.compile(r"[A-Z][A-Za-z.'’\-]*")


def _place_state_repl(m: re.Match[str]) -> str:
    prefix = m.group("prefix")
    st = m.group("st")
    alias = _STATE_MAP.get(st.upper())
    if not alias:
        return m.group(0)

    words = _PLACE_WORD_RE.findall(prefix.rstrip(" ,"))
    if not words:
        return m.group(0)

    last_word = words[-1]
    last_upper = last_word.upper()

    # Peek at following text so we can be stricter for ambiguous codes.
    tail = m.string[m.end() : m.end() + 16]

    # Extra guardrail for ambiguous state codes.
    if st.upper() in _AMBIGUOUS_STATE_CODES:
        if last_upper in _PLACE_STOPWORDS:
            return m.group(0)

        # If the next token is a connector like "to" / "and", be stricter.
        if re.match(r"\s+(?:to|and)\b", tail, re.IGNORECASE):
            if len(words) == 1 and last_upper in _PLACE_STOPWORDS:
                return m.group(0)

    return f'{prefix}<vtml_sub alias="{alias}">{st}</vtml_sub>'


_RULES: list[Rule] = [
    # Wind-direction ranges such as "W to SW winds" need both abbreviations
    # expanded.  The following direct-before-"winds" rule handles only SW;
    # this companion rule handles the leading W without broadening direction
    # substitutions in ordinary prose.
    Rule(
        re.compile(
            r"\b(?P<dir>NNE|ENE|ESE|SSE|SSW|WSW|WNW|NNW|NE|NW|SE|SW|N|S|E|W)\b"
            r"(?=\s+to\s+(?:NNE|ENE|ESE|SSE|SSW|WSW|WNW|NNW|NE|NW|SE|SW|N|S|E|W)"
            r"(?=\s*(?:wind(?:s|['’]s)?\b|[.,;!?])))",
            re.IGNORECASE,
        ),
        _wind_dir_repl,
    ),
    Rule(
        re.compile(
            r"(?P<prefix>\bto\s+)"
            r"(?P<dir>NNE|ENE|ESE|SSE|SSW|WSW|WNW|NNW|NE|NW|SE|SW|N|S|E|W)\b"
            r"(?=\s*(?:wind(?:s|['’]s)?\b|[.,;!?]))",
            re.IGNORECASE,
        ),
        _wind_range_end_repl,
    ),
    # --- Common wind-direction abbreviations, only in "NW winds ..." contexts ---
    Rule(
        re.compile(
            r"\b(?P<dir>NNE|ENE|ESE|SSE|SSW|WSW|WNW|NNW|NE|NW|SE|SW|N|S|E|W)\b(?=\s+wind(?:s|['’]s)?\b)",
            re.IGNORECASE,
        ),
        _wind_dir_repl,
    ),
    # --- The big one: "winds / wind's" homograph (verb vs weather noun) ---
    # Force weather-noun "winds" and "wind's" => /wɪndz/
    # Avoid common verb cases like "winds up ..." (except "up to ...") and "winds down" / "winds its ...".
    Rule(
        re.compile(
            r"\b(?:winds|wind['’]s)\b"
            r"(?!\s+up\b(?!\s+to\b))"
            r"(?!\s+down\b)"
            r"(?!\s+(?:its|their|his|her|my|your|our)\b)",
            re.IGNORECASE,
        ),
        _phoneme_x_cmu("W IH1 N D Z"),
    ),
    # Bare singular "wind" (noun) => /wɪnd/
    # Mirror the same exclusions as the winds/wind's rule below.
    Rule(
        re.compile(
            r"\bwind\b"
            r"(?!\s+up\b(?!\s+to\b))"
            r"(?!\s+down\b)"
            r"(?!\s+(?:its|their|his|her|my|your|our)\b)",
            re.IGNORECASE,
        ),
        _phoneme_x_cmu("W IH1 N D"),
    ),
    # --- Units (spoken nicely) ---
    Rule(re.compile(r"\bmph\b", re.IGNORECASE), _sub_alias("miles per hour")),
    Rule(re.compile(r"\bkts\b", re.IGNORECASE), _sub_alias("knots")),
    Rule(re.compile(r"\bkt\b", re.IGNORECASE), _sub_alias("knots")),
    Rule(re.compile(r"\bhpa\b", re.IGNORECASE), _sub_alias("hecto pascals")),
    Rule(re.compile(r"\bmb\b", re.IGNORECASE), _sub_alias("millibars")),
    # A terminal period marks the end of the unit, not a place-name suffix.
    # Handle it before the guarded bare-unit rules so "3 ft. Chance" still
    # becomes "3 feet" when the next sentence starts with a capital letter.
    Rule(
        re.compile(r"(\d+(?:\.\d+)?)\s*(ft\.)(?=\s|$|[,:;!?])", re.IGNORECASE),
        lambda m: f'{m.group(1)} <vtml_sub alias="feet">{m.group(2)}</vtml_sub>',
    ),
    # Degree symbol forms
    Rule(re.compile(r"°\s*F\b"), _sub_alias("degrees Fahrenheit")),
    Rule(re.compile(r"°\s*C\b"), _sub_alias("degrees Celsius")),
    # Measurements only when they look like units after a number.
    #
    # The negative lookahead (?![ \t]+(?-i:[A-Z])) blocks the rule when the abbreviation
    # is followed by same-line whitespace and an uppercase letter — i.e. a proper noun,
    # place name, or state code.  We intentionally restrict this to horizontal
    # whitespace so a line break after a unit (common in wrapped marine zone text like
    # "60 NM.\nWaters ...") does not suppress the conversion.  (?-i:[A-Z]) uses an
    # inline flag to disable IGNORECASE for just that character class so it only
    # matches true uppercase letters.
    #
    # Without this guard the "in" rule fires on constructions like:
    #   "Interstate 270 in Maryland" → "Interstate 270 inches Maryland"
    # The positive lookahead (?=\s|$|[,:;!?]) is kept as the word-boundary guard so
    # we never match mid-word (e.g. "inches" itself isn't double-converted).
    Rule(
        re.compile(r"(\d+(?:\.\d+)?)\s*((?:in)\.?)(?![ \t]+(?-i:[A-Z]))(?=\s|$|[,:;!?])", re.IGNORECASE),
        lambda m: f'{m.group(1)} <vtml_sub alias="inches">{m.group(2)}</vtml_sub>',
    ),
    Rule(
        re.compile(r"(\d+(?:\.\d+)?)\s*((?:ft)\.?)(?![ \t]+(?-i:[A-Z]))(?=\s|$|[,:;!?])", re.IGNORECASE),
        lambda m: f'{m.group(1)} <vtml_sub alias="feet">{m.group(2)}</vtml_sub>',
    ),
    Rule(
        re.compile(r"(\d+(?:\.\d+)?)\s*((?:mi)\.?)(?![ \t]+(?-i:[A-Z]))(?=\s|$|[,:;!?])", re.IGNORECASE),
        lambda m: f'{m.group(1)} <vtml_sub alias="miles">{m.group(2)}</vtml_sub>',
    ),
    Rule(
        re.compile(r"(\d+(?:\.\d+)?)\s*((?:nm)\.?)(?![ \t]+(?-i:[A-Z]))(?=\s|$|[,:;!?])", re.IGNORECASE),
        lambda m: f'{m.group(1)} <vtml_sub alias="nautical miles">{m.group(2)}</vtml_sub>',
    ),
    # State abbreviations only when they look like place-name suffixes.
    Rule(_PLACE_STATE_RE, _place_state_repl),
    # --- A few NWS-ish abbreviations that show up in headers/closures ---
    # NOAA: VoiceText Paul reads it as "N-O-A-A" letters without this rule.
    Rule(re.compile(r"\bNOAA\b"), _sub_alias("noah")),
    Rule(re.compile(r"\bNWS\b"), _sub_alias("National Weather Service")),
    Rule(re.compile(r"\bSAME\b"), _sub_alias("same")),
    Rule(re.compile(r"\bthunderstorms\b", re.IGNORECASE), _phoneme_x_cmu("TH AH1 N D ER0 S T OW0 R M Z")),
    Rule(re.compile(r"\bthunderstorm\b", re.IGNORECASE), _phoneme_x_cmu("TH AH1 N D ER0 S T OW0 R M")),
    Rule(re.compile(r"\bTSTMS\b", re.IGNORECASE), _phoneme_x_cmu("TH AH1 N D ER0 S T OW0 R M Z")),
    Rule(re.compile(r"\bTSTM\b", re.IGNORECASE), _phoneme_x_cmu("TH AH1 N D ER0 S T OW0 R M")),
    # Time zone abbreviations that may still appear in headers or time announcements
    Rule(re.compile(r"\bEST\b", re.IGNORECASE), _sub_alias("Eastern Standard Time")),
    Rule(re.compile(r"\bEDT\b", re.IGNORECASE), _sub_alias("Eastern Daylight Time")),
    # Aviation categories sometimes appear in AFD/TAF-style text
    Rule(re.compile(r"\bVFR\b"), _sub_alias("V F R")),
    Rule(re.compile(r"\bMVFR\b"), _sub_alias("M V F R")),
    Rule(re.compile(r"\bIFR\b"), _sub_alias("I F R")),
    Rule(re.compile(r"\bLIFR\b"), _sub_alias("L I F R")),
    # --- Impact / Impacts ---
    # Paul mispronounces the meteorological header "Impact:" (he stresses the wrong
    # syllable as a verb).  The "impakt" / "impakts" alias spelling forces correct
    # noun stress.  Source: VoiceText Paul Phoneme Guide + LWX TTS doc.
    Rule(re.compile(r"\bimpacts\b", re.IGNORECASE), _sub_alias("impakts")),
    Rule(re.compile(r"\bimpact\b", re.IGNORECASE), _sub_alias("impakt")),
    # --- Tornadic ---
    # Paul defaults to an odd schwa-heavy reading.  Phoneme: "tor-NAY-dik".
    # Variant 1 (generic / no office-specific quirks).
    # Source: VoiceText Paul Phoneme Guide.
    Rule(re.compile(r"\btornadic\b", re.IGNORECASE), _phoneme_x_cmu("T AO R N AE1 D IH K")),
    # --- Projectiles ---
    # Without this Paul places no stress on "-tiles", making it nearly
    # unintelligible.  LWX-confirmed phoneme from VoiceText Paul Phoneme Guide.
    Rule(re.compile(r"\bprojectiles\b", re.IGNORECASE), _phoneme_x_cmu("P R UH0 JH EH0 K T AY2 L Z")),
    # --- Objects ---
    # Paul reads "objects" with a weak initial vowel unless forced.
    # Phoneme leads with AA (the "AH" in "father") for the NWR-correct "AHbjects" onset.
    # Source: LWX TTS doc (x-CMU).
    Rule(re.compile(r"\bobjects\b", re.IGNORECASE), _phoneme_x_cmu("AA AH0 B JH EH0 K T S")),
    # --- Fog ---
    # Paul tends to blend "fog" into the following word without a hard boundary.
    # A zero-duration pause acts as a word-boundary separator without audible gap.
    # Source: LWX TTS doc convention.
    Rule(re.compile(r"\bfog\b", re.IGNORECASE), _word_with_trailing_pause(0)),
]


def _awips_break_to_vtml(match: re.Match[str]) -> str:
    strength = match.group(1).strip().lower()
    milliseconds = {
        "none": 0,
        "x-weak": 75,
        "weak": 125,
        "medium": 250,
        "strong": 500,
        "x-strong": 750,
    }.get(strength, 0)
    return f'<vtml_pause time="{milliseconds}"/>'


def _awips_plain_text(value: str) -> str:
    rendered = _AWIPS_BREAK_RE.sub(_awips_break_to_vtml, value)
    return re.sub(r"\s+", " ", rendered).strip()


def _awips_alias_text(value: str) -> str:
    """Return an AWIPS textual substitute without carrying break markup."""
    return re.sub(r"\s+", " ", _AWIPS_BREAK_RE.sub(" ", value)).strip()


def _awips_word_pattern(word: str) -> Pattern[str]:
    pieces: list[str] = []
    digit_index = 0
    for char in word:
        if char == "#":
            pieces.append(f"(?P<awips_digit_{digit_index}>[0-9])")
            digit_index += 1
        else:
            pieces.append(re.escape(char))
    return re.compile(
        r"(?<![A-Za-z0-9])" + "".join(pieces) + r"(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def _awips_hash_substitution(value: str, match: re.Match[str]) -> str:
    rendered: list[str] = []
    digit_index = 0
    for char in value:
        if char == "#":
            try:
                captured = match.group(f"awips_digit_{digit_index}")
            except (IndexError, KeyError):
                captured = None
            rendered.append(captured if captured is not None else "#")
            digit_index += 1
        else:
            rendered.append(char)
    return "".join(rendered)


def _awips_phoneme_part(
    template: str,
    tag: re.Match[str],
    index: int,
    tags: list[re.Match[str]],
    source_words: list[str],
    consumed: int,
    cursor: int,
) -> tuple[str, int]:
    prefix = _awips_plain_text(template[cursor : tag.start()])
    suffix = _awips_plain_text(template[tag.end() :])
    suffix_words = len(re.sub(r"<vtml_pause time=\"[0-9]+\"/>", " ", suffix).split())
    remaining_tags = len(tags) - index - 1
    available = len(source_words) - consumed - suffix_words
    if suffix_words == 0 and remaining_tags:
        available -= remaining_tags
    matched_words = source_words[consumed : consumed + max(0, available)]
    phonemes = " ".join(tag.group(1).split())
    if matched_words and phonemes:
        rendered = f'<vtml_phoneme alphabet="x-cmu" ph="{phonemes}">{" ".join(matched_words)}</vtml_phoneme>'
    else:
        rendered = " ".join(matched_words)
    part = " ".join(value for value in (prefix, rendered) if value)
    return part, consumed + len(matched_words)


def _awips_phoneme_replacement(template: str, source: str) -> str:
    tags = list(_AWIPS_PHONEME_RE.finditer(template))
    if not tags:
        return _awips_plain_text(template)

    source_words = source.split()
    consumed = 0
    cursor = 0
    output: list[str] = []
    for index, tag in enumerate(tags):
        part, consumed = _awips_phoneme_part(
            template,
            tag,
            index,
            tags,
            source_words,
            consumed,
            cursor,
        )
        if part:
            output.append(part)
        cursor = tag.end()

    tail = _awips_plain_text(template[cursor:])
    if tail:
        output.append(tail)
    return " ".join(part for part in output if part).strip()


def _awips_rule_factory(substitute: str) -> Callable[[re.Match[str]], str]:
    def _replace(match: re.Match[str]) -> str:
        rendered = _awips_hash_substitution(substitute, match)
        if _AWIPS_PHONEME_RE.search(rendered):
            return _awips_phoneme_replacement(rendered, match.group(0))
        alias = _awips_alias_text(rendered)
        if not alias:
            return match.group(0)
        return _sub_alias(alias)(match)

    return _replace


def _awips_national_entries() -> list[dict[str, object]]:
    try:
        resource = resources.files("seasonalweather.tts").joinpath("awips_paul_national.json")
        payload = cast(object, json.loads(resource.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    typed_payload = cast(dict[str, object], payload)
    raw_entries = typed_payload.get("entries", [])
    if not isinstance(raw_entries, list):
        return []
    return [cast(dict[str, object], item) for item in raw_entries if isinstance(item, dict)]


def _awips_rule_for_entry(entry: dict[str, object]) -> Rule | None:
    word = str(entry.get("word", "") or "")
    substitute = str(entry.get("substitute", "") or "")
    if not word or not substitute:
        return None
    # A hand-maintained SeasonalWeather rule is authoritative even when
    # it matches only a word inside an AWIPS phrase (for example, the
    # local ``fog`` boundary rule).  Do not let the additive dictionary
    # wrap or replace that earlier result.
    if any(rule.rx.search(word) is not None for rule in _RULES):
        return None
    return Rule(_awips_word_pattern(word), _awips_rule_factory(substitute))


@lru_cache(maxsize=1)
def _awips_national_rules() -> tuple[Rule, ...]:
    """Load AWIPS-II's national Paul dictionary as additive defaults.

    The checked-in resource is the BMH ``paul-nat`` dictionary.  SeasonalWeather
    rules are applied first, so local phoneme and abbreviation decisions retain
    precedence over this upstream baseline.
    """
    rules: list[Rule] = []
    for entry in sorted(
        _awips_national_entries(),
        key=lambda item: -len(str(item.get("word", ""))),
    ):
        rule = _awips_rule_for_entry(entry)
        if rule is not None:
            rules.append(rule)
    return tuple(rules)


def _user_rule_flags(spec: dict) -> int:
    return re.IGNORECASE if bool(spec.get("ignore_case", False)) else 0


def _compile_user_rule_rx(spec: dict) -> Pattern[str]:
    match = str(spec.get("match", "") or "")
    if not match:
        raise ValueError("override is missing 'match'")
    if len(match) > MAX_CONFIGURED_REGEX_PATTERN:
        raise ValueError("override pattern is overlong")
    if bool(spec.get("regex", False)):
        return compile_safe_regex(match, flags=_user_rule_flags(spec))
    return compile_safe_regex(re.escape(match), flags=_user_rule_flags(spec))


def _build_user_rules(
    alias_overrides: list[dict] | None = None,
    phoneme_overrides_x_cmu: list[dict] | None = None,
) -> list[Rule]:
    if len(alias_overrides or []) > MAX_CONFIGURED_REGEX_RULES:
        raise ValueError("too many VoiceText alias overrides")
    if len(phoneme_overrides_x_cmu or []) > MAX_CONFIGURED_REGEX_RULES:
        raise ValueError("too many VoiceText phoneme overrides")
    # Phonemes first, then aliases, so a manual phoneme can win cleanly.
    return [
        *_build_user_rule_group(phoneme_overrides_x_cmu, "ph", "phoneme", _phoneme_x_cmu),
        *_build_user_rule_group(alias_overrides, "alias", "alias", _sub_alias),
    ]


def _build_user_rule_group(
    specs: list[dict] | None,
    replacement_key: str,
    label: str,
    replacement_factory: Callable[[str], Callable[[re.Match[str]], str]],
) -> list[Rule]:
    rules: list[Rule] = []
    for spec in specs or []:
        if not isinstance(spec, dict):
            raise ValueError(f"VoiceText {label} override must be an object")
        replacement = str(spec.get(replacement_key, "") or "").strip()
        if not replacement:
            raise ValueError(f"VoiceText {label} override is missing '{replacement_key}'")
        if len(replacement) > MAX_CONFIGURED_REGEX_REPLACEMENT:
            raise ValueError(f"VoiceText {label} replacement is overlong")
        validate_replacement(replacement)
        rx = _compile_user_rule_rx(spec)
        rules.append(Rule(rx, replacement_factory(replacement), True))
    return rules


def apply_voicetext_paul_vtml(
    text: str,
    vtml_lexicon: bool = True,
    alias_overrides: list[dict] | None = None,
    phoneme_overrides_x_cmu: list[dict] | None = None,
) -> str:
    """
    Apply small, meteorology-focused VTML tweaks.
    - Does NOT touch existing <...> tags.
    - No newlines are introduced (your wrapper still flattens anyway).
    - vtml_lexicon: set False to skip built-in substitutions.
    - alias_overrides / phoneme_overrides_x_cmu: optional user-configured rules.
    """
    if not text:
        return ""

    rules = _build_user_rules(
        alias_overrides=alias_overrides,
        phoneme_overrides_x_cmu=phoneme_overrides_x_cmu,
    )
    if vtml_lexicon:
        rules.extend(_RULES)
        rules.extend(_awips_national_rules())

    if not rules:
        return text

    rendered = text
    for r in rules:
        tags: list[str] = []

        def _stash_tag(match: re.Match[str], tag_list: list[str] = tags) -> str:
            tag_list.append(match.group(0))
            return f"\x00SW_TAG_{len(tag_list) - 1}\x00"

        protected = _VTML_BLOCK_RE.sub(_stash_tag, rendered)
        protected = _TAG_SPLIT_RE.sub(_stash_tag, protected)
        parts = re.split(r"(\x00SW_TAG_[0-9]+\x00)", protected)
        for i in range(0, len(parts), 2):  # even indexes = plain text between tags
            parts[i], count = r.rx.subn(r.repl, parts[i], count=MAX_CONFIGURED_REGEX_REPLACEMENTS + 1)
            if r.user_configured and count > MAX_CONFIGURED_REGEX_REPLACEMENTS:
                raise ValueError("VoiceText override replacement work exceeded its bound")

        rendered = "".join(parts)
        for index, tag in enumerate(tags):
            rendered = rendered.replace(f"\x00SW_TAG_{index}\x00", tag)

    return rendered
