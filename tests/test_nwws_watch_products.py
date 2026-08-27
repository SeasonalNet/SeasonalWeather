import datetime as dt
from zoneinfo import ZoneInfo

from seasonalweather.broadcast.product_text import (
    build_nwws_watch_action_script,
    build_nwws_watch_vtec_script,
    build_nwws_watch_partial_cancel_script,
    extract_nwws_wcn_area_desc,
    match_nwws_wcn_area_same,
)
from seasonalweather.same.ugc import extract_ugc_zones


WCN_SVA_NEW = """WWUS61 KLWX 201639
WCNLWX

WATCH COUNTY NOTIFICATION FOR WATCH 234
NATIONAL WEATHER SERVICE BALTIMORE MD/WASHINGTON DC
1239 PM EDT WED MAY 20 2026

DCC001-MDC031-VAC013-210000-
/O.NEW.KLWX.SV.A.0234.260520T1639Z-260521T0000Z/

THE NATIONAL WEATHER SERVICE HAS ISSUED SEVERE THUNDERSTORM WATCH
234 IN EFFECT UNTIL 8 PM EDT THIS EVENING FOR THE FOLLOWING
AREAS

THE DISTRICT OF COLUMBIA

IN MARYLAND THIS WATCH INCLUDES 1 COUNTY

IN CENTRAL MARYLAND

MONTGOMERY

IN VIRGINIA THIS WATCH INCLUDES 1 COUNTY

IN NORTHERN VIRGINIA

ARLINGTON

THIS INCLUDES THE CITIES OF ARLINGTON AND ROCKVILLE.

$$
"""


def test_wcn_ugc_county_block_is_parseable():
    assert extract_ugc_zones(WCN_SVA_NEW) == ["DCC001", "MDC031", "VAC013"]


def test_wcn_sva_new_uses_watch_specific_narration():
    script = build_nwws_watch_vtec_script(
        WCN_SVA_NEW,
        ["/O.NEW.KLWX.SV.A.0234.260520T1639Z-260521T0000Z/"],
        local_tz=ZoneInfo("America/New_York"),
        now=dt.datetime(2026, 5, 20, 12, 39, tzinfo=ZoneInfo("America/New_York")),
    )

    assert "The National Weather Service has issued Severe Thunderstorm Watch Number 234." in script
    assert "Effective until 8 PM this evening." in script
    assert "This watch includes the District of Columbia." in script
    assert "in Maryland: Montgomery" in script
    assert "in Virginia: Arlington" in script
    assert "WATCH COUNTY NOTIFICATION" not in script
    assert "DCC001" not in script
    assert "End of message." not in script


def test_wcn_watch_script_prefers_resolved_area_text_when_available():
    script = build_nwws_watch_vtec_script(
        WCN_SVA_NEW,
        ["/O.NEW.KLWX.SV.A.0234.260520T1639Z-260521T0000Z/"],
        local_tz=ZoneInfo("America/New_York"),
        area_text="Montgomery, MD; Arlington, VA",
        now=dt.datetime(2026, 5, 20, 12, 39, tzinfo=ZoneInfo("America/New_York")),
    )

    assert "in Maryland: Montgomery" in script
    assert "in Virginia: Arlington" in script
    assert "the District of Columbia" not in script


WCN_SVA_NO_UGC = """WWUS61 KPHI 201810
WCNPHI

WATCH COUNTY NOTIFICATION FOR WATCH 235
NATIONAL WEATHER SERVICE MOUNT HOLLY NJ
210 PM EDT WED MAY 20 2026

/O.NEW.KPHI.SV.A.0235.260520T1810Z-260521T0100Z/

THE NATIONAL WEATHER SERVICE HAS ISSUED SEVERE THUNDERSTORM WATCH
235 IN EFFECT UNTIL 9 PM EDT THIS EVENING FOR THE FOLLOWING
AREAS

IN DELAWARE THIS WATCH INCLUDES 3 COUNTIES

KENT              NEW CASTLE        SUSSEX

IN MARYLAND THIS WATCH INCLUDES 4 COUNTIES

CAROLINE          KENT              QUEEN ANNE'S
TALBOT

THIS INCLUDES THE CITIES OF DOVER, GEORGETOWN, AND WILMINGTON.

$$
"""


def test_wcn_watch_without_ugc_can_format_text_but_has_no_same_target_source():
    """Regression fixture for live KPHI.SV.A.0235: CAP must not be deduped by a SAME-less WCN FULL."""
    assert extract_ugc_zones(WCN_SVA_NO_UGC) == []

    script = build_nwws_watch_vtec_script(
        WCN_SVA_NO_UGC,
        ["/O.NEW.KPHI.SV.A.0235.260520T1810Z-260521T0100Z/"],
        local_tz=ZoneInfo("America/New_York"),
        now=dt.datetime(2026, 5, 20, 14, 10, tzinfo=ZoneInfo("America/New_York")),
    )

    assert "The National Weather Service has issued Severe Thunderstorm Watch Number 235." in script
    assert "Effective until 9 PM tonight." in script
    assert "in Delaware: Kent, New Castle, and Sussex" in script
    assert "in Maryland: Caroline, Kent, Queen Anne's, and Talbot" in script


def test_wcn_watch_without_ugc_can_recover_service_area_same_from_county_block():
    area_desc = extract_nwws_wcn_area_desc(WCN_SVA_NO_UGC)

    assert "Caroline, MD" in area_desc
    assert "Kent, MD" in area_desc
    assert "Queen Anne's, MD" in area_desc
    assert "Talbot, MD" in area_desc

    matched = match_nwws_wcn_area_same(
        area_desc,
        {
            "024011": "Caroline, MD",
            "024029": "Kent, MD",
            "024035": "Queen Anne's, MD",
            "024041": "Talbot, MD",
            "024031": "Montgomery, MD",
        },
    )

    assert matched == ["024011", "024029", "024035", "024041"]


WCN_SVA_MIXED_CAN_CON = """WWUS61 KLWX 202041
WCNLWX

WATCH COUNTY NOTIFICATION FOR WATCH 234
NATIONAL WEATHER SERVICE BALTIMORE MD/WASHINGTON DC
441 PM EDT WED MAY 20 2026

MDC001-023-043-210000-
/O.CAN.KLWX.SV.A.0234.000000T0000Z-260521T0000Z/

THE NATIONAL WEATHER SERVICE HAS CANCELLED SEVERE THUNDERSTORM
WATCH 234 FOR THE FOLLOWING AREAS

IN MARYLAND THIS CANCELS 3 COUNTIES

IN NORTH CENTRAL MARYLAND
WASHINGTON

IN WESTERN MARYLAND
ALLEGANY                       GARRETT

THIS INCLUDES THE CITIES OF CUMBERLAND, FROSTBURG, HAGERSTOWN,
AND OAKLAND.

$$

DCC001-MDC003-005-013-015-021-025-027-031-033-510-
VAC013-043-047-059-061-069-079-107-113-139-153-157-
165-171-187-510-600-610-660-210000-
/O.CON.KLWX.SV.A.0234.000000T0000Z-260521T0000Z/

SEVERE THUNDERSTORM WATCH 234 REMAINS VALID UNTIL 8 PM EDT THIS
EVENING FOR THE FOLLOWING AREAS

THE DISTRICT OF COLUMBIA

IN MARYLAND THIS WATCH INCLUDES 10 COUNTIES

IN CENTRAL MARYLAND
ANNE ARUNDEL                       HOWARD
MONTGOMERY                         PRINCE GEORGES

IN NORTH CENTRAL MARYLAND
CARROLL                       FREDERICK

IN NORTHEAST MARYLAND
CECIL

IN NORTHERN MARYLAND
BALTIMORE BALTIMORE CITY HARFORD

IN VIRGINIA THIS WATCH INCLUDES 4 COUNTIES

IN NORTHERN VIRGINIA
CLARKE                       FAUQUIER
LOUDOUN                      PRINCE WILLIAM

THIS INCLUDES THE CITIES OF ANNAPOLIS, BALTIMORE, FREDERICK,
LEESBURG, MANASSAS, WASHINGTON, AND WESTMINSTER.

$$

ANZ530>532-535-538-539-210000-
/O.CON.KLWX.SV.A.0234.000000T0000Z-260521T0000Z/

SEVERE THUNDERSTORM WATCH 234 REMAINS VALID UNTIL 8 PM EDT THIS
EVENING FOR THE FOLLOWING AREAS

THIS WATCH INCLUDES THE FOLLOWING ADJACENT COASTAL WATERS
CHESAPEAKE BAY NORTH OF POOLES ISLAND MD
CHESAPEAKE BAY FROM POOLES ISLAND TO SANDY POINT MD

$$
"""


def test_wcn_mixed_can_con_uses_watch_specific_partial_script():
    script = build_nwws_watch_partial_cancel_script(
        WCN_SVA_MIXED_CAN_CON,
        [
            "/O.CAN.KLWX.SV.A.0234.000000T0000Z-260521T0000Z/",
            "/O.CON.KLWX.SV.A.0234.000000T0000Z-260521T0000Z/",
        ],
        local_tz=ZoneInfo("America/New_York"),
        now=dt.datetime(2026, 5, 20, 16, 41, tzinfo=ZoneInfo("America/New_York")),
    )

    assert "Severe Thunderstorm Watch Number 234 has been cancelled" in script
    assert "in Maryland: Washington, Allegany, and Garrett" in script
    assert (
        "Severe Thunderstorm Watch Number 234 remains in effect until 8 PM this evening for the District of Columbia and the following counties:"
        in script
    )
    assert "This watch includes the District of Columbia." not in script
    assert "in Virginia: Clarke, Fauquier, Loudoun, and Prince William" in script
    assert "404 WWUS61" not in script
    assert "WATCH COUNTY NOTIFICATION" not in script
    assert "DCC001" not in script
    assert "MDC001" not in script
    assert "THE NATIONAL WEATHER SERVICE HAS CANCELLED" not in script
    assert "End of message." not in script


WCN_SVA_MIXED_EXP_CON = """WWUS61 KLWX 120204
WCNLWX

WATCH COUNTY NOTIFICATION FOR WATCHES 315/317
NATIONAL WEATHER SERVICE BALTIMORE MD/WASHINGTON DC
1004 PM EDT THU JUN 11 2026

MDC003-005-025-510-120315-
/O.EXP.KLWX.SV.A.0315.000000T0000Z-260612T0200Z/

THE NATIONAL WEATHER SERVICE HAS ALLOWED SEVERE THUNDERSTORM
WATCH 315 TO EXPIRE FOR THE FOLLOWING AREAS

IN MARYLAND THIS ALLOWS TO EXPIRE 4 COUNTIES

IN CENTRAL MARYLAND

ANNE ARUNDEL

IN NORTHERN MARYLAND

BALTIMORE
BALTIMORE CITY
HARFORD

THIS INCLUDES THE CITIES OF ABERDEEN, ANNAPOLIS, AND BALTIMORE.

$$

MDC009-015-017-037-VAC099-179-120400-
/O.CON.KLWX.SV.A.0317.000000T0000Z-260612T0400Z/

SEVERE THUNDERSTORM WATCH 317 REMAINS VALID UNTIL MIDNIGHT EDT
TONIGHT FOR THE FOLLOWING AREAS

IN MARYLAND THIS WATCH INCLUDES 4 COUNTIES

IN NORTHEAST MARYLAND

CECIL

IN SOUTHERN MARYLAND

CALVERT                       CHARLES
ST. MARYS

IN VIRGINIA THIS WATCH INCLUDES 2 COUNTIES

IN CENTRAL VIRGINIA

KING GEORGE

IN NORTHERN VIRGINIA

STAFFORD

THIS INCLUDES THE CITIES OF ANDORA, BARKSDALE, AND WALDORF.

$$
"""


def test_wcn_mixed_exp_con_uses_lifecycle_county_wording_and_midnight_tonight():
    script = build_nwws_watch_partial_cancel_script(
        WCN_SVA_MIXED_EXP_CON,
        [
            "/O.EXP.KLWX.SV.A.0315.000000T0000Z-260612T0200Z/",
            "/O.CON.KLWX.SV.A.0317.000000T0000Z-260612T0400Z/",
        ],
        local_tz=ZoneInfo("America/New_York"),
        now=dt.datetime(2026, 6, 11, 22, 4, tzinfo=ZoneInfo("America/New_York")),
    )

    assert (
        "Severe Thunderstorm Watch Number 315 has been allowed to expire for the following counties: in Maryland: Anne Arundel, Baltimore, Baltimore City, and Harford."
        in script
    )
    assert (
        "Severe Thunderstorm Watch Number 317 remains in effect until midnight tonight for the following counties: in Maryland: Cecil, Calvert, Charles, and St. Marys; in Virginia: King George and Stafford."
        in script
    )
    assert "has been allowed to expire for the following areas.\n\nThis watch includes" not in script
    assert "Remember, a severe thunderstorm watch means" in script


def test_nwws_render_facade_normalizes_wcn_watch_script():
    from seasonalweather.broadcast.product_text import render_nwws_product_script

    rendered = render_nwws_product_script(
        product_type="WCN",
        base_script="WATCH COUNTY NOTIFICATION raw fallback.",
        official_text=WCN_SVA_NEW,
        vtec=["/O.NEW.KLWX.SV.A.0234.260520T1639Z-260521T0000Z/"],
        vtec_actions={"NEW"},
        has_tracks=True,
        should_full=False,
        event_text="Severe Thunderstorm Watch",
        area_text="",
        headline="",
        local_tz=ZoneInfo("America/New_York"),
    )

    assert rendered.changed is True
    assert rendered.renderer == "nwws-watch-vtec"
    assert "Severe Thunderstorm Watch Number 234" in rendered.script
    assert "WATCH COUNTY NOTIFICATION raw fallback" not in rendered.script


def test_nwws_render_facade_normalizes_wcn_partial_cancel_script():
    from seasonalweather.broadcast.product_text import render_nwws_product_script

    rendered = render_nwws_product_script(
        product_type="WCN",
        base_script="raw partial cancel fallback.",
        official_text=WCN_SVA_MIXED_CAN_CON,
        vtec=[
            "/O.CAN.KLWX.SV.A.0234.000000T0000Z-260521T0000Z/",
            "/O.CON.KLWX.SV.A.0234.000000T0000Z-260521T0000Z/",
        ],
        vtec_actions={"CAN", "CON"},
        has_tracks=True,
        should_full=False,
        event_text="Severe Thunderstorm Watch",
        area_text="",
        headline="",
        local_tz=ZoneInfo("America/New_York"),
    )

    assert rendered.changed is True
    assert rendered.renderer == "nwws-watch-partial-cancel"
    assert "Severe Thunderstorm Watch Number 234 has been cancelled" in rendered.script
    assert "raw partial cancel fallback" not in rendered.script


WCN_SVA_CAN_NEW_SAME_SECTION = """WWUS61 KLWX 201901
WCNLWX

WATCH COUNTY NOTIFICATION FOR WATCHES 604/605
NATIONAL WEATHER SERVICE BALTIMORE MD/WASHINGTON DC
301 PM EDT THU AUG 20 2026

DCC001-MDC031-210200-
/O.CAN.KLWX.SV.A.0604.000000T0000Z-260821T0200Z/
/O.NEW.KLWX.TO.A.0605.260820T1901Z-260821T0200Z/

THE NATIONAL WEATHER SERVICE HAS ISSUED TORNADO WATCH 605 UNTIL
10 PM EDT THIS EVENING WHICH REPLACES A PORTION OF SEVERE
THUNDERSTORM WATCH 604. THE NEW WATCH IS VALID FOR THE FOLLOWING
AREAS

THE DISTRICT OF COLUMBIA

IN MARYLAND THE NEW WATCH INCLUDES 1 COUNTY

IN CENTRAL MARYLAND

MONTGOMERY

$$
"""


def test_wcn_can_and_new_in_one_section_render_as_separate_action_scripts():
    vtec = [
        "/O.CAN.KLWX.SV.A.0604.000000T0000Z-260821T0200Z/",
        "/O.NEW.KLWX.TO.A.0605.260820T1901Z-260821T0200Z/",
    ]

    full = build_nwws_watch_action_script(WCN_SVA_CAN_NEW_SAME_SECTION, vtec, "NEW")
    voice = build_nwws_watch_action_script(WCN_SVA_CAN_NEW_SAME_SECTION, vtec, "CAN")

    assert "Tornado Watch Number 605" in full
    assert "Severe Thunderstorm Watch Number 604 has been cancelled" not in full
    assert "Severe Thunderstorm Watch Number 604 has been cancelled" in voice
    assert "Tornado Watch Number 605" not in voice


WCN_SVA_COMPLETE_CAN = """WWUS61 KLWX 050129
WCNLWX

WATCH COUNTY NOTIFICATION FOR WATCH 457
NATIONAL WEATHER SERVICE BALTIMORE MD/WASHINGTON DC
929 PM EDT SAT JUL 4 2026

DCC001-MDC003-005-009-015-017-025-027-031-033-037-510-VAC013-059-
099-153-177-179-510-600-610-630-683-685-050230-
/O.CAN.KLWX.SV.A.0457.000000T0000Z-260705T0200Z/

THE NATIONAL WEATHER SERVICE HAS CANCELLED SEVERE THUNDERSTORM
WATCH 457 FOR THE FOLLOWING AREAS

THE DISTRICT OF COLUMBIA

IN MARYLAND THIS CANCELS 11 COUNTIES

IN CENTRAL MARYLAND

ANNE ARUNDEL                       HOWARD
MONTGOMERY                         PRINCE GEORGES

IN NORTHEAST MARYLAND

CECIL

IN NORTHERN MARYLAND

BALTIMORE
BALTIMORE CITY
HARFORD

IN SOUTHERN MARYLAND

CALVERT                       CHARLES
ST. MARYS

IN VIRGINIA THIS CANCELS 12 COUNTIES

IN CENTRAL VIRGINIA

CITY OF FREDERICKSBURG
KING GEORGE
SPOTSYLVANIA

IN NORTHERN VIRGINIA

ARLINGTON
CITY OF ALEXANDRIA
CITY OF FAIRFAX
CITY OF FALLS CHURCH
CITY OF MANASSAS
CITY OF MANASSAS PARK
FAIRFAX
PRINCE WILLIAM
STAFFORD

THIS INCLUDES THE CITIES OF ALEXANDRIA, ANNAPOLIS, BALTIMORE,
FREDERICKSBURG, MANASSAS, WASHINGTON, AND WOODBRIDGE.

$$

ANZ530>543-050230-
/O.CAN.KLWX.SV.A.0457.000000T0000Z-260705T0200Z/

THE NATIONAL WEATHER SERVICE HAS CANCELLED SEVERE THUNDERSTORM
WATCH 457 FOR THE FOLLOWING AREAS

THIS CANCELS THE FOLLOWING ADJACENT COASTAL WATERS

CHESAPEAKE BAY NORTH OF POOLES ISLAND MD
CHESAPEAKE BAY FROM POOLES ISLAND TO SANDY POINT MD
TIDAL POTOMAC FROM KEY BRIDGE TO INDIAN HEAD MD

$$
"""


WCN_SVA_COMPLETE_EXP = """WWUS61 KLWX 050200
WCNLWX

WATCH COUNTY NOTIFICATION FOR WATCH 458
NATIONAL WEATHER SERVICE BALTIMORE MD/WASHINGTON DC
1000 PM EDT SAT JUL 4 2026

MDC009-017-037-050300-
/O.EXP.KLWX.SV.A.0458.000000T0000Z-260705T0200Z/

THE NATIONAL WEATHER SERVICE HAS ALLOWED SEVERE THUNDERSTORM
WATCH 458 TO EXPIRE FOR THE FOLLOWING AREAS

IN MARYLAND THIS ALLOWS TO EXPIRE 3 COUNTIES

IN SOUTHERN MARYLAND

CALVERT                       CHARLES
ST. MARYS

$$
"""


def test_wcn_complete_can_omits_watch_definition_reminder():
    vtec = [
        "/O.CAN.KLWX.SV.A.0457.000000T0000Z-260705T0200Z/",
        "/O.CAN.KLWX.SV.A.0457.000000T0000Z-260705T0200Z/",
    ]
    script = build_nwws_watch_vtec_script(
        WCN_SVA_COMPLETE_CAN,
        vtec,
        local_tz=ZoneInfo("America/New_York"),
        now=dt.datetime(2026, 7, 4, 21, 29, tzinfo=ZoneInfo("America/New_York")),
    )

    assert "Severe Thunderstorm Watch Number 457 has been cancelled" in script
    assert "Remember, a severe thunderstorm watch means" not in script
    assert "conditions are favorable for the development of severe weather" not in script


def test_wcn_complete_expiration_omits_watch_definition_reminder():
    script = build_nwws_watch_vtec_script(
        WCN_SVA_COMPLETE_EXP,
        ["/O.EXP.KLWX.SV.A.0458.000000T0000Z-260705T0200Z/"],
        local_tz=ZoneInfo("America/New_York"),
        now=dt.datetime(2026, 7, 4, 22, 0, tzinfo=ZoneInfo("America/New_York")),
    )

    assert "Severe Thunderstorm Watch Number 458 has been allowed to expire" in script
    assert "Remember, a severe thunderstorm watch means" not in script
    assert "conditions are favorable for the development of severe weather" not in script
