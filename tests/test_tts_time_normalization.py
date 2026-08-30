from seasonalweather.tts.tts import clean_for_tts, normalize_nws_dual_time_zones, normalize_nws_spoken_times


def test_normalize_nws_spoken_times_adds_colons_to_compact_local_times() -> None:
    text = "At 651 PM EDT, storms remain in effect until 700 PM EDT."

    assert normalize_nws_spoken_times(text) == (
        "At 6:51 PM EDT, storms remain in effect until 7:00 PM EDT."
    )


def test_normalize_nws_spoken_times_handles_noon_midnight_style_hours() -> None:
    text = "The watch is in effect from 1200 AM to 115 PM and again at 1015 PM."

    assert normalize_nws_spoken_times(text) == (
        "The watch is in effect from 12:00 AM to 1:15 PM and again at 10:15 PM."
    )


def test_normalize_nws_spoken_times_leaves_utc_and_vtec_timestamps_alone() -> None:
    text = "/O.CON.KLWX.SV.W.0041.000000T0000Z-260513T2300Z/ TIME...MOT...LOC 2251Z"

    assert normalize_nws_spoken_times(text) == text


def test_normalize_nws_dual_time_zones_makes_awips_forms_speakable() -> None:
    assert normalize_nws_dual_time_zones("651 PM EDT (551 PM CDT)") == "651 PM EDT, OR 551 PM CDT"
    assert normalize_nws_dual_time_zones("651 PM EDT / 551 PM CDT") == "651 PM EDT, OR 551 PM CDT"


def test_clean_for_tts_drops_mixed_case_and_dual_zone_issued_headers() -> None:
    text = "556 PM edt (456 PM cdt) Thu Aug 27 2026\nThe warning is active."

    assert clean_for_tts(text) == "The warning is active."


def test_clean_for_tts_normalizes_alert_times_before_synthesis() -> None:
    text = "At 651 PM EDT, severe thunderstorms were located near Winchester until 700 PM EDT."

    assert clean_for_tts(text) == (
        "At 6:51 PM EDT, severe thunderstorms were located near Winchester until 7:00 PM EDT."
    )


def test_clean_for_tts_drops_undelimited_nws_machine_readable_block() -> None:
    text = """The warning has been cancelled.
LAT...LON 3876 7633 3884 7627
      3879 7622 3879 7619
TIME...MOT...LOC 2352Z 235DEG 29KT 3866 7628
$$
MPS
"""

    assert clean_for_tts(text) == "The warning has been cancelled."


def test_clean_for_tts_sentence_cases_svs_headline_and_expands_saint() -> None:
    text = (
        "...THE SEVERE THUNDERSTORM WARNING FOR NORTH CENTRAL ST. MARYS "
        "WILL EXPIRE AT 800 PM EDT..."
    )

    spoken = clean_for_tts(text)

    assert spoken == (
        "The severe thunderstorm warning for north central Saint Marys "
        "will expire at 8:00 PM EDT."
    )
    assert " AT " not in spoken
