from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Marine SAME "SS" values are not state/territory FIPS codes.  Do not treat
# 0SS000 values in these ranges as state-wide wildcard matches.  They can still
# match when explicitly present in the configured allow list.  Values that also
# collide with official territory FIPS values are intentionally omitted here.
MARINE_SAME_SS: frozenset[str] = frozenset({
    "70",  # Lake Superior
    "71",  # Gulf of Mexico
    "73",  # Atlantic coastal north of ~31N
    "75",  # Pacific coastal California
    "76",  # Pacific coastal Oregon/Washington
    "77",  # Alaska coastal
    "79",  # Pacific Islands
    "81",  # Lake Ontario
    "82",  # Lake Huron
    "83",  # Lake Erie
})


# 000000 is the national/all-US SAME location.  SeasonalWeather must not treat
# it as an ordinary service-area hit, even if someone accidentally adds it to a
# local allow list.
NATIONAL_SAME_LOCATION = "000000"

# Only these EAS/SAME event codes have inherently nationwide purpose.  RWT and
# RMT are tests, but their broadcasts remain station/state/area scoped.
NATIONAL_SCOPE_EVENT_CODES: frozenset[str] = frozenset({"EAN", "EAT", "NPT"})


def normalize_same_location(value: Any) -> str | None:
    """Return a normalized 6-digit SAME PSSCCC code, or None if malformed."""
    if value is None:
        return None
    digits = "".join(ch for ch in str(value).strip() if ch.isdigit())
    if len(digits) != 6:
        return None
    return digits


def normalize_same_locations(values: Iterable[Any] | None) -> list[str]:
    """Normalize a SAME location sequence while preserving order and de-duping."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        code = normalize_same_location(raw)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def normalize_same_allow_set(values: Iterable[Any] | None) -> set[str]:
    """Normalize a configured SAME service-area allow list."""
    return set(normalize_same_locations(values))


def same_state_fips(code: Any) -> str | None:
    """Return the SS portion of PSSCCC, or None for malformed input."""
    same6 = normalize_same_location(code)
    if not same6:
        return None
    return same6[1:3]


def is_national_same_location(code: Any) -> bool:
    """True for the national/all-US SAME location that must not wildcard-match."""
    return normalize_same_location(code) == NATIONAL_SAME_LOCATION


def is_national_scope_event_code(code: Any) -> bool:
    """True only for event codes whose EAS purpose is inherently nationwide."""
    return str(code or "").strip().upper() in NATIONAL_SCOPE_EVENT_CODES


def is_statewide_same_location(code: Any) -> bool:
    """
    True for ordinary state/territory-wide SAME locations: 0SS000.

    This intentionally excludes 000000 and known marine SAME SS values.  Marine
    0SS000 locations may still be allowed by exact allow-list membership, but
    are not treated as state-wide wildcards for county service areas.
    """
    same6 = normalize_same_location(code)
    if not same6:
        return False
    if same6 == NATIONAL_SAME_LOCATION:
        return False
    ss = same6[1:3]
    return same6[0] == "0" and ss != "00" and ss not in MARINE_SAME_SS and same6[3:] == "000"


def _service_area_has_state(allow_set: set[str], state_fips: str) -> bool:
    for allowed in allow_set:
        if allowed == NATIONAL_SAME_LOCATION:
            continue
        if normalize_same_location(allowed) != allowed:
            continue
        if allowed[1:3] != state_fips:
            continue
        if allowed[3:] == "000":
            continue
        # Exact state-wide entries are handled before wildcard matching.  For
        # wildcard matching, require a concrete local county/city location so
        # config containing only 024000 does not also admit every Maryland county.
        if is_statewide_same_location(allowed):
            continue
        return True
    return False


def same_location_matches_service_area(
    code: Any,
    allow_set: Iterable[Any] | set[str],
    *,
    allow_statewide_input: bool = True,
    allow_national: bool = False,
    event_code: Any | None = None,
) -> bool:
    """
    Return True when one SAME location is within the configured service area.

    Exact allow-list membership always matches, except 000000 unless
    ``allow_national`` is true for a nationwide event code.  When
    ``allow_statewide_input`` is true, an incoming 0SS000 state-wide code also
    matches if the service area contains at least one concrete county/city SAME
    location from that state.  The location itself is not expanded; callers
    that relay SAME should preserve the original 0SS000 code.
    """
    same6 = normalize_same_location(code)
    if not same6:
        return False
    if same6 == NATIONAL_SAME_LOCATION:
        return allow_national and is_national_scope_event_code(event_code)

    normalized_allow = allow_set if isinstance(allow_set, set) else normalize_same_allow_set(allow_set)
    if same6 in normalized_allow:
        return True

    if not allow_statewide_input or not is_statewide_same_location(same6):
        return False

    state = same_state_fips(same6)
    return bool(state and _service_area_has_state(normalized_allow, state))


def same_locations_intersect_service_area(
    codes: Iterable[Any] | None,
    allow_set: Iterable[Any] | set[str],
    *,
    allow_statewide_input: bool = True,
    allow_national: bool = False,
    event_code: Any | None = None,
) -> bool:
    """Return True when any SAME location intersects the service area."""
    codes_list = list(codes or [])
    normalized_allow = allow_set if isinstance(allow_set, set) else normalize_same_allow_set(allow_set)
    if (
        allow_national
        and is_national_scope_event_code(event_code)
        and any(is_national_same_location(code) for code in codes_list)
    ):
        return True
    if not normalized_allow:
        return False
    return any(
        same_location_matches_service_area(
            code,
            normalized_allow,
            allow_statewide_input=allow_statewide_input,
            allow_national=allow_national,
            event_code=event_code,
        )
        for code in codes_list
    )


def filter_same_locations_to_service_area(
    codes: Iterable[Any] | None,
    allow_set: Iterable[Any] | set[str],
    *,
    allow_statewide_input: bool = True,
    allow_national: bool = False,
    event_code: Any | None = None,
) -> list[str]:
    """
    Return in-service SAME locations, preserving received codes and order.

    State-wide 0SS000 locations are kept as 0SS000 when they match by state.
    They are not expanded into county lists.
    """
    codes_list = list(codes or [])
    normalized_allow = allow_set if isinstance(allow_set, set) else normalize_same_allow_set(allow_set)
    if (
        allow_national
        and is_national_scope_event_code(event_code)
        and any(is_national_same_location(raw) for raw in codes_list)
    ):
        return [NATIONAL_SAME_LOCATION]
    return [
        code
        for code in normalize_same_locations(codes_list)
        if same_location_matches_service_area(
            code,
            normalized_allow,
            allow_statewide_input=allow_statewide_input,
            allow_national=allow_national,
            event_code=event_code,
        )
    ]
