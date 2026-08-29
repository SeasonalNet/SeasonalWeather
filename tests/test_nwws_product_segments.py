from seasonalweather.alerts.vtec import VTEC_FIND_RE, toneout_policy
from seasonalweather.broadcast.product_text import (
    build_nwws_partial_cancel_script,
    build_nwws_terminal_cancel_expiry_script,
    expiry_summary_script,
    parse_nwws_product_segments,
)
from seasonalweather.tts.tts import clean_for_tts


PARTIAL_CAN_CON_SVS = """403
WWUS51 KLWX 132251
SVSLWX

Severe Weather Statement
National Weather Service Baltimore MD/Washington DC
651 PM EDT Wed May 13 2026

WVC027-065-132301-
/O.CAN.KLWX.SV.W.0041.000000T0000Z-260513T2300Z/
Morgan WV-Hampshire WV-
651 PM EDT Wed May 13 2026

...THE SEVERE THUNDERSTORM WARNING FOR SOUTHEASTERN MORGAN AND
SOUTHEASTERN HAMPSHIRE COUNTIES IS CANCELLED...

The storms which prompted the warning have moved out of the warned
area. Therefore, the warning has been cancelled.

&&

LAT...LON 3957 7783 3956 7784 3956 7789 3954 7787
TIME...MOT...LOC 2251Z 279DEG 35KT 3951 7802 3925 7815 3910 7839

$$

VAC043-069-840-WVC003-037-132300-
/O.CON.KLWX.SV.W.0041.000000T0000Z-260513T2300Z/
Frederick VA-Clarke VA-City of Winchester VA-Jefferson WV-
Berkeley WV-
651 PM EDT Wed May 13 2026

...A SEVERE THUNDERSTORM WARNING REMAINS IN EFFECT UNTIL 700 PM EDT
FOR SOUTHERN FREDERICK AND NORTHWESTERN CLARKE COUNTIES IN
NORTHWESTERN VIRGINIA...CENTRAL JEFFERSON AND CENTRAL BERKELEY
COUNTIES IN THE PANHANDLE OF WEST VIRGINIA AND THE CITY OF
WINCHESTER...

At 651 PM EDT, severe thunderstorms were located along a line
extending from near Martinsburg to near Winchester to near Star
Tannery, moving east at 40 mph.

HAZARD...60 mph wind gusts and quarter size hail.

SOURCE...Radar indicated.

IMPACT...Damaging winds will cause some trees and large branches to
         fall. This could injure those outdoors, as well as damage
         homes and vehicles.

Locations impacted include...
Winchester, Martinsburg, Charles Town, Shepherdstown, Millwood Pike,
Ranson, Berryville, Inwood, Stephens City, Kearneysville.

PRECAUTIONARY/PREPAREDNESS ACTIONS...

For your protection move to an interior room on the lowest floor of a
building.

&&

LAT...LON 3957 7783 3956 7784 3956 7789 3954 7787
TIME...MOT...LOC 2251Z 279DEG 35KT 3951 7802 3925 7815 3910 7839

HAIL THREAT...RADAR INDICATED
MAX HAIL SIZE...1.00 IN
WIND THREAT...RADAR INDICATED
MAX WIND GUST...60 MPH

$$

KLW
"""


def test_partial_can_con_vtec_policy_tracks_both_actions():
    vtec = VTEC_FIND_RE.findall(PARTIAL_CAN_CON_SVS)
    policy = toneout_policy(vtec)

    assert vtec == [
        "/O.CAN.KLWX.SV.W.0041.000000T0000Z-260513T2300Z/",
        "/O.CON.KLWX.SV.W.0041.000000T0000Z-260513T2300Z/",
    ]
    assert policy.mode == "VOICE"
    assert policy.same_code is None
    assert policy.cancel_tracks == frozenset({"KLWX.SV.W.0041"})
    assert policy.continuation_tracks == frozenset({"KLWX.SV.W.0041"})


def test_partial_can_con_svs_segments_include_cancel_and_continuation_text():
    segments = parse_nwws_product_segments(PARTIAL_CAN_CON_SVS)

    assert len(segments) == 2
    assert segments[0].actions == {"CAN"}
    assert segments[0].area_text == "Morgan WV; Hampshire WV"
    assert "warning has been cancelled" in segments[0].reason_text

    assert segments[1].actions == {"CON"}
    assert segments[1].area_text == "Frederick VA; Clarke VA; City of Winchester VA; Jefferson WV; Berkeley WV"
    assert segments[1].expiry_phrase == "700 PM EDT"
    assert "60 mph wind gusts and quarter size hail" in segments[1].reason_text
    assert "For your protection move to an interior room" in segments[1].precautions
    assert "Locations impacted include: Winchester, Martinsburg" in segments[1].reason_text


def test_partial_can_con_script_says_cancelled_and_remains_in_effect():
    segments = parse_nwws_product_segments(PARTIAL_CAN_CON_SVS)
    script = build_nwws_partial_cancel_script("Severe Thunderstorm Warning", segments)

    assert "has been cancelled for the following areas: Morgan WV; Hampshire WV" not in script
    assert "The severe thunderstorm warning for southeastern morgan and southeastern hampshire counties is cancelled." in script
    assert "A severe thunderstorm warning remains in effect until 700 PM EDT" in script
    assert "The Severe Thunderstorm Warning remains in effect" not in script
    assert "Locations impacted include: Winchester, Martinsburg" in script
    assert "End of message." not in script

KCTP_PARTIAL_CAN_CON_SVS = """930
WWUS51 KCTP 201726
SVSCTP

Severe Weather Statement
National Weather Service State College PA
126 PM EDT Wed May 20 2026

PAC057-201745-
/O.CAN.KCTP.SV.W.0064.000000T0000Z-260520T1745Z/
Fulton PA-
126 PM EDT Wed May 20 2026

...THE SEVERE THUNDERSTORM WARNING FOR FULTON COUNTY IS CANCELLED...

CANCELLED

The severe thunderstorm which prompted the warning has moved out of
The warned area. Therefore, the warning has been cancelled.
A Severe Thunderstorm Watch remains in effect until 800 PM EDT for
south central Pennsylvania.

&&

LAT...LON 3990 7800
TIME...MOT...LOC 1725Z 260DEG 22KT 3980 7800

$$

PAC055-201745-
/O.CON.KCTP.SV.W.0064.000000T0000Z-260520T1745Z/
Franklin PA-
126 PM EDT Wed May 20 2026

...A SEVERE THUNDERSTORM WARNING REMAINS IN EFFECT UNTIL 145 PM EDT
FOR SOUTHWESTERN FRANKLIN COUNTY...

FOR SOUTHWESTERN FRANKLIN COUNTY...

At 125 PM EDT, a severe thunderstorm was located over Claylick,
moving east at 25 mph.

HAZARD...60 mph wind gusts and quarter size hail.

SOURCE...Radar indicated.

IMPACT...Hail damage to vehicles is expected. Expect wind damage to
         roofs, siding, and trees.

Locations impacted include...
St. Thomas, Mercersburg, Claylick, Williamson, Upton, and Whitetail
Ski Area.

PRECAUTIONARY/PREPAREDNESS ACTIONS...

Stay inside a well built structure and keep away from windows.

&&

LAT...LON 3990 7800
TIME...MOT...LOC 1725Z 260DEG 22KT 3980 7800

$$
"""


def test_partial_can_con_svs_preserves_sentence_boundaries_and_skips_labels():
    segments = parse_nwws_product_segments(KCTP_PARTIAL_CAN_CON_SVS)
    script = build_nwws_partial_cancel_script("Severe Thunderstorm Warning", segments)

    assert "CANCELLED The severe thunderstorm" not in script
    assert "warning has been cancelled. A Severe Thunderstorm Watch" in script
    assert "COUNTY At 125 PM" not in script
    assert "moving east at 25 mph. Hazard: 60 mph wind gusts" in script
    assert "Hazard: 60 mph wind gusts and quarter size hail. Source: Radar indicated. Impact: Hail damage" in script
    assert "Expect wind damage to roofs, siding, and trees." in script
    assert "Stay inside a well built structure and keep away from windows." in script



FLS_CAN_FA_W = """147
WGUS81 KLWX 271737
FLSLWX

Flood Statement
National Weather Service Baltimore MD/Washington DC
137 PM EDT Wed May 27 2026

MDC001-271747-
/O.CAN.KLWX.FA.W.0003.000000T0000Z-260527T1900Z/
/00000.0.ER.000000T0000Z.000000T0000Z.000000T0000Z.OO/
Allegany MD-
137 PM EDT Wed May 27 2026

...FLOOD WARNING IS CANCELLED...

The Flood Warning is cancelled for a portion of western Maryland,
including the following area, Allegany.

Flood waters have receded. The heavy rain has ended. Flooding is no
longer expected to pose a threat. Please continue to heed remaining
road closures.

&&

LAT...LON 3955 7899 3953 7899 3951 7899 3950 7901
      3947 7903 3948 7904 3948 7905 3947 7905
      3947 7906 3948 7906 3951 7905 3954 7903
      3956 7903 3955 7901

$$

KJP
"""


def test_fls_terminal_cancel_keeps_scoped_cancellation_prose():
    assert expiry_summary_script(FLS_CAN_FA_W) == "The heavy rain has ended."

    script = build_nwws_terminal_cancel_expiry_script("Flood Warning", FLS_CAN_FA_W)

    assert "The Flood Warning is cancelled for a portion of western Maryland" in script
    assert "including the following area, Allegany" in script
    assert "Flood waters have receded" in script
    assert "Flooding is no longer expected to pose a threat" in script
    assert "Please continue to heed remaining road closures" in script
    assert "Flood warning is cancelled.\nThe Flood Warning is cancelled" not in script
    assert "End of message." not in script

EXP_SVS = """246
WWUS51 KLWX 201855
SVSLWX

Severe Weather Statement
National Weather Service Baltimore MD/Washington DC
255 PM EDT Wed May 20 2026

VAC069-WVC027-201904-
/O.EXP.KLWX.SV.W.0053.000000T0000Z-260520T1900Z/
Frederick VA-Hampshire WV-
255 PM EDT Wed May 20 2026

...THE SEVERE THUNDERSTORM WARNING FOR WEST CENTRAL FREDERICK COUNTY
IN NORTHWESTERN VIRGINIA AND SOUTHEASTERN HAMPSHIRE COUNTIES IN
EASTERN WEST VIRGINIA WILL EXPIRE AT 300 PM EDT...

The storm which prompted the warning has weakened below severe
limits, and no longer poses an immediate threat to life or property.
Therefore, the warning will be allowed to expire.

A Severe Thunderstorm Watch remains in effect until 800 PM EDT for
northwestern Virginia...and eastern West Virginia.

&&

LAT...LON 3917 7858 3923 7860 3935 7838 3917 7832
TIME...MOT...LOC 1854Z 253DEG 18KT 3924 7842

$$

Belak
"""


def test_expiry_summary_script_sentence_cases_all_caps_svs_headline_for_tts():
    script = expiry_summary_script(EXP_SVS)

    assert script is not None
    assert "WILL EXPIRE AT" not in script
    assert "will expire at 300 PM EDT." in script

    spoken = clean_for_tts(script)
    assert "will expire at 3:00 PM EDT." in spoken
    assert " AT " not in spoken


WRAPPED_LIFECYCLE_EXP_SVS = """659
WWUS51 KLWX 272156 RRA
SVSLWX

Severe Weather Statement
National Weather Service Baltimore MD/Washington DC
556 PM EDT Thu Aug 27 2026

VAC015-139-165-660-272206-
/O.EXP.KLWX.SV.W.0450.000000T0000Z-260827T2200Z/
Rockingham VA-Page VA-Augusta VA-City of Harrisonburg VA-
556 PM EDT Thu Aug 27 2026

...THE SEVERE THUNDERSTORM WARNING FOR SOUTHEASTERN ROCKINGHAM...
SOUTHWESTERN PAGE...AND NORTHEASTERN AUGUSTA COUNTIES AND THE CITY OF
HARRISONBURG WILL EXPIRE AT 600 PM EDT...

The storm which prompted the warning has weakened below severe
limits, and no longer poses an immediate threat to life or property.
Therefore, the warning will be allowed to expire.  However, heavy
rain is still possible with this thunderstorm.

A Severe Thunderstorm Watch remains in effect until 1000 PM EDT for
central, western and northwestern Virginia.

To report severe weather, contact your nearest law enforcement
agency. They will relay your report to the National Weather Service
Sterling Virginia.

&&

LAT...LON 3852 7886 3848 7862 3847 7862 3845 7856
      3843 7855 3842 7849 3835 7856 3833 7856
      3833 7859 3831 7861 3829 7866 3825 7866
      3825 7871 3822 7874 3814 7878 3831 7905
TIME...MOT...LOC 2156Z 307DEG 17KT 3832 7869

$$

Belak
"""


def test_wrapped_lifecycle_headline_consumes_the_whole_nonblank_block() -> None:
    segments = parse_nwws_product_segments(WRAPPED_LIFECYCLE_EXP_SVS)

    assert len(segments) == 1
    segment = segments[0]
    assert segment.headline == (
        "The severe thunderstorm warning for southeastern rockingham... "
        "southwestern page...and northeastern augusta counties and the city of "
        "harrisonburg will expire at 600 PM EDT"
    )
    assert segment.reason_text.startswith("The storm which prompted the warning")
    assert "SOUTHWESTERN PAGE" not in segment.reason_text
    assert "will expire at 600 PM EDT" not in segment.reason_text


def test_wrapped_lifecycle_headline_stays_sentence_cased_through_paul_vtml() -> None:
    from seasonalweather.alerts.builder import build_spoken_alert
    from seasonalweather.alerts.product import parse_product_text
    from seasonalweather.broadcast.product_text import render_nws_product_script
    from seasonalweather.tts.voicetext_paul_vtml import apply_voicetext_paul_vtml

    parsed = parse_product_text(WRAPPED_LIFECYCLE_EXP_SVS)
    assert parsed is not None
    spoken = build_spoken_alert(parsed, WRAPPED_LIFECYCLE_EXP_SVS)
    rendered = render_nws_product_script(
        product_type="SVS",
        base_script=spoken.script,
        official_text=WRAPPED_LIFECYCLE_EXP_SVS,
        vtec=["/O.EXP.KLWX.SV.W.0450.000000T0000Z-260827T2200Z/"],
        vtec_actions={"EXP"},
        has_tracks=True,
        should_full=False,
        event_text="Severe Thunderstorm Warning",
        area_text="",
        headline="",
    )

    paul_input = apply_voicetext_paul_vtml(rendered.script)

    assert rendered.renderer == "nwws-detailed-terminal-cancel-expiry"
    assert "SOUTHWESTERN PAGE" not in rendered.script
    assert "will expire at 600 PM EDT" in rendered.script
    assert " AT " not in paul_input

KLWX_CON_SVS = """435
WWUS51 KLWX 201903
SVSLWX

Severe Weather Statement
National Weather Service Baltimore MD/Washington DC
303 PM EDT Wed May 20 2026

VAC139-171-201915-
/O.CON.KLWX.SV.W.0054.000000T0000Z-260520T1915Z/
Shenandoah VA-Page VA-
303 PM EDT Wed May 20 2026

...A SEVERE THUNDERSTORM WARNING REMAINS IN EFFECT UNTIL 315 PM EDT
FOR SOUTH CENTRAL SHENANDOAH AND NORTH CENTRAL PAGE COUNTIES...

At 303 PM EDT, a severe thunderstorm was located near Luray, or 9
miles south of Woodstock, moving east at 20 mph.

HAZARD...60 mph wind gusts.

SOURCE...Radar indicated.

IMPACT...Damaging winds will cause some trees and large branches to
         fall. This could injure those outdoors, as well as damage
         homes and vehicles. Roadways may become blocked by downed
         trees. Localized power outages are possible. Unsecured
         light objects may become projectiles.

Locations impacted include...
Luray and Kings Crossing.

PRECAUTIONARY/PREPAREDNESS ACTIONS...

For your protection move to an interior room on the lowest floor of a
building.

&&

LAT...LON 3866 7857 3873 7858 3881 7846 3867 7842
TIME...MOT...LOC 1903Z 253DEG 18KT 3874 7851

$$

Belak
"""


def test_spoken_alert_sentence_cases_all_caps_svs_continuation_body_for_tts():
    from seasonalweather.alerts.builder import build_spoken_alert
    from seasonalweather.alerts.product import parse_product_text

    parsed = parse_product_text(KLWX_CON_SVS)
    assert parsed is not None

    spoken = build_spoken_alert(parsed, KLWX_CON_SVS)

    assert "FOR SOUTH CENTRAL" not in spoken.script
    assert "A SEVERE THUNDERSTORM WARNING" not in spoken.script
    assert "Shenandoah VA-Page VA-" not in spoken.script
    assert "For south central shenandoah" in spoken.script
    assert "Hazard: 60 mph wind gusts." in spoken.script
    assert "Source: Radar indicated." in spoken.script
    assert "Impact: Damaging winds will cause" in spoken.script


KLWX_PARTIAL_CAN_CON_SVS = """704
WWUS51 KLWX 202021
SVSLWX

Severe Weather Statement
National Weather Service Baltimore MD/Washington DC
421 PM EDT Wed May 20 2026

VAC043-202031-
/O.CAN.KLWX.SV.W.0060.000000T0000Z-260520T2045Z/
Clarke VA-
421 PM EDT Wed May 20 2026

...THE SEVERE THUNDERSTORM WARNING FOR SOUTHEASTERN CLARKE COUNTY IS
CANCELLED...

The severe thunderstorm which prompted the warning has moved out of
The warned area. Therefore, the warning has been cancelled.
A Severe Thunderstorm Watch remains in effect until 800 PM EDT for
northern Virginia.

&&

LAT...LON 3895 7783 3907 7788 3913 7757 3898 7748
TIME...MOT...LOC 2021Z 255DEG 15KT 3901 7776

$$

VAC061-107-202045-
/O.CON.KLWX.SV.W.0060.000000T0000Z-260520T2045Z/
Loudoun VA-Fauquier VA-
421 PM EDT Wed May 20 2026

...A SEVERE THUNDERSTORM WARNING REMAINS IN EFFECT UNTIL 445 PM EDT
FOR CENTRAL LOUDOUN AND NORTH CENTRAL FAUQUIER COUNTIES...

At 421 PM EDT, a severe thunderstorm was located over Middleburg, or
12 miles west of Brambleton, moving east at 20 mph.

HAZARD...60 mph wind gusts.

SOURCE...Radar indicated.

IMPACT...Damaging winds will cause some trees and large branches to
         fall. This could injure those outdoors, as well as damage
         homes and vehicles. Roadways may become blocked by downed
         trees. Localized power outages are possible. Unsecured
         light objects may become projectiles.

Locations impacted include...
Leesburg, Broadlands, Brambleton, Ashburn, Middleburg, Oatlands,
Saint Louis, Gleedsville, Aldie, Philomont, and Hughesville.

PRECAUTIONARY/PREPAREDNESS ACTIONS...

For your protection move to an interior room on the lowest floor of a
building.

&&

LAT...LON 3895 7783 3907 7788 3913 7757 3898 7748
TIME...MOT...LOC 2021Z 255DEG 15KT 3901 7776

HAIL THREAT...RADAR INDICATED
MAX HAIL SIZE...<.75 IN
WIND THREAT...RADAR INDICATED
MAX WIND GUST...60 MPH

$$

Belak
"""


def test_partial_can_con_klwx_preserves_nws_headlines_and_locations():
    segments = parse_nwws_product_segments(KLWX_PARTIAL_CAN_CON_SVS)
    script = build_nwws_partial_cancel_script("Severe Thunderstorm Warning", segments)

    assert "CANCELLED..." not in script
    assert "The severe thunderstorm warning for southeastern clarke county is cancelled." in script
    assert "The Severe Thunderstorm Warning has been cancelled for the following areas" not in script
    assert "A severe thunderstorm warning remains in effect until 445 PM EDT for central loudoun and north central fauquier counties." in script
    assert "The Severe Thunderstorm Warning remains in effect until 445 PM EDT for the following areas" not in script
    assert "Locations impacted include: Leesburg, Broadlands" in script
    assert "For your protection move to an interior room" in script


def test_nwws_render_facade_normalizes_sps_preamble():
    from seasonalweather.broadcast.product_text import render_nwws_product_script

    official = """WWUS81 KLWX 141930
SPSLWX

Special Weather Statement
National Weather Service Baltimore MD/Washington DC
330 PM EDT Sun Jun 14 2026

MDC031-142000-

A strong thunderstorm will impact Montgomery County.
"""

    rendered = render_nwws_product_script(
        product_type="SPS",
        base_script="This is a statement from the National Weather Service. Special Weather Statement. A strong thunderstorm will impact Montgomery County.",
        official_text=official,
        vtec=[],
        vtec_actions=set(),
        has_tracks=False,
        should_full=False,
        event_text="Special Weather Statement",
        area_text="Montgomery, MD",
        headline="",
    )

    assert rendered.changed is True
    assert rendered.renderer == "nwws-sps-preamble"
    assert rendered.script.startswith(
        "And now a Special Weather Statement from your National Weather Service, issued at 3:30 PM Eastern Daylight Time Sunday June 14 2026."
    )
    assert "Special Weather Statement. A strong" not in rendered.script


KPHI_UNDELIMITED_MACHINE_BLOCK_SVS = """542
WWUS51 KPHI 042354
SVSPHI

Severe Weather Statement
National Weather Service Mount Holly NJ
754 PM EDT Sat Jul 4 2026

MDC011-035-041-050004-
/O.CAN.KPHI.SV.W.0131.000000T0000Z-260705T0030Z/
Talbot MD-Queen Anne's MD-Caroline MD-
754 PM EDT Sat Jul 4 2026

...THE SEVERE THUNDERSTORM WARNING FOR TALBOT...SOUTH CENTRAL QUEEN
ANNE'S AND SOUTHWESTERN CAROLINE COUNTIES IS CANCELLED...

The storm which prompted the warning has weakened below severe
limits, and no longer poses an immediate threat to life or property.
Therefore, the warning has been cancelled.

A Severe Thunderstorm Watch remains in effect until 1100 PM EDT for
eastern and northeastern Maryland.

To report severe weather, contact your nearest law enforcement
agency. They will relay your report to the National Weather Service
Mount Holly NJ.

LAT...LON 3876 7633 3884 7627 3879 7622 3879 7619
      3889 7620 3898 7609 3869 7590 3857 7603
      3863 7608 3864 7615 3869 7618 3868 7621
      3872 7624 3876 7622 3871 7628 3878 7629
      3877 7632 3867 7634 3874 7634 3875 7638
TIME...MOT...LOC 2352Z 235DEG 29KT 3866 7628

$$

MPS
"""


def test_terminal_svs_drops_undelimited_machine_readable_block() -> None:
    script = build_nwws_terminal_cancel_expiry_script(
        "Severe Thunderstorm Warning",
        KPHI_UNDELIMITED_MACHINE_BLOCK_SVS,
    )

    assert "warning has been cancelled" in script
    assert "National Weather Service Mount Holly NJ" in script
    assert "LAT...LON" not in script
    assert "TIME...MOT...LOC" not in script
    assert "3876" not in script
    assert "2352Z" not in script


KLWX_ST_MARYS_EXP_SVS = """510
WWUS51 KLWX 042357
SVSLWX

Severe Weather Statement
National Weather Service Baltimore MD/Washington DC
757 PM EDT Sat Jul 4 2026

MDC009-037-050007-
/O.EXP.KLWX.SV.W.0212.000000T0000Z-260705T0000Z/
St. Marys MD-Calvert MD-
757 PM EDT Sat Jul 4 2026

...THE SEVERE THUNDERSTORM WARNING FOR NORTH CENTRAL ST. MARYS AND
SOUTHERN CALVERT COUNTIES WILL EXPIRE AT 800 PM EDT...

The storm which prompted the warning was moving out of the area.
Therefore, the warning will be allowed to expire. However, gusty
winds are still possible with this thunderstorm.

A Severe Thunderstorm Watch remains in effect until 1000 PM EDT for
southern Maryland.

&&

LAT...LON 3835 7659 3845 7671 3852 7649 3843 7640
TIME...MOT...LOC 2357Z 227DEG 22KT 3854 7642

$$
Manning
"""


def test_terminal_svs_sentence_cases_headline_and_expands_saint() -> None:
    script = build_nwws_terminal_cancel_expiry_script(
        "Severe Thunderstorm Warning",
        KLWX_ST_MARYS_EXP_SVS,
    )

    assert "Saint Marys" in script
    assert "ST. MARYS" not in script
    assert "WILL EXPIRE AT" not in script
    assert "will expire at 800 PM EDT" in script

    spoken = clean_for_tts(script)
    assert "Saint Marys" in spoken
    assert "will expire at 8:00 PM EDT" in spoken
    assert " AT " not in spoken
