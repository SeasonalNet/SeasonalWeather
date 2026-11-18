from seasonalweather.same.locations import (
    filter_same_locations_to_service_area,
    is_statewide_same_location,
    same_location_matches_service_area,
    same_locations_intersect_service_area,
)


def test_statewide_same_location_detection_excludes_national_and_marine() -> None:
    assert is_statewide_same_location("024000")
    assert is_statewide_same_location("042000")
    assert not is_statewide_same_location("000000")
    assert not is_statewide_same_location("073000")
    assert not is_statewide_same_location("024031")
    assert not is_statewide_same_location("124000")


def test_statewide_input_matches_configured_county_in_same_state() -> None:
    allow = {"024031", "051059"}
    assert same_location_matches_service_area("024000", allow)
    assert same_location_matches_service_area("051000", allow)
    assert not same_location_matches_service_area("042000", allow)


def test_statewide_input_is_preserved_not_expanded() -> None:
    allow = {"024031", "024033", "051059"}
    assert filter_same_locations_to_service_area(["024000", "051059"], allow) == ["024000", "051059"]


def test_national_same_never_matches_even_if_configured() -> None:
    allow = {"000000", "024031"}
    assert not same_location_matches_service_area("000000", allow)
    assert not same_locations_intersect_service_area(["000000"], allow)
    assert filter_same_locations_to_service_area(["000000", "024000"], allow) == ["024000"]


def test_national_same_is_opt_in_for_national_source_paths() -> None:
    allow = {"024031"}
    assert same_location_matches_service_area("000000", allow, allow_national=True, event_code="NPT")
    assert same_locations_intersect_service_area(["000000"], allow, allow_national=True, event_code="NPT")
    assert filter_same_locations_to_service_area(
        ["000000", "024031"],
        allow,
        allow_national=True,
        event_code="NPT",
    ) == ["000000"]


def test_national_same_does_not_bypass_for_local_event_code() -> None:
    allow = {"024031"}
    assert not same_location_matches_service_area("000000", allow, allow_national=True, event_code="TOR")
    assert not same_locations_intersect_service_area(["000000"], allow, allow_national=True, event_code="TOR")
    assert filter_same_locations_to_service_area(
        ["000000", "024031"],
        allow,
        allow_national=True,
        event_code="TOR",
    ) == ["024031"]


def test_configured_statewide_code_does_not_admit_all_counties_when_wildcard_disabled() -> None:
    allow = {"024000"}
    assert same_location_matches_service_area("024000", allow, allow_statewide_input=False)
    assert not same_location_matches_service_area("024031", allow, allow_statewide_input=False)
