from seasonalweather.tts.voicetext_paul_vtml import apply_voicetext_paul_vtml


def test_nm_expands_across_wrapped_marine_zone_lines() -> None:
    text = (
        "Waters from Cape May NJ to Fenwick Island DE from 20 to 60 NM.\n"
        "Waters from Great Egg Inlet NJ to Cape May NJ from 20 to 60 NM.\n"
    )

    rendered = apply_voicetext_paul_vtml(text)

    assert rendered.count('alias="nautical miles"') == 2


def test_nm_still_expands_in_sentence_context() -> None:
    text = "At 1241 PM EDT, a severe thunderstorm was located 25 nm southeast of Deepwater Reef."

    rendered = apply_voicetext_paul_vtml(text)

    assert '25 <vtml_sub alias="nautical miles">nm</vtml_sub>' in rendered


def test_in_rule_still_avoids_place_name_false_positive() -> None:
    text = "Interstate 270 in Maryland remains busy."

    rendered = apply_voicetext_paul_vtml(text)

    assert "inches" not in rendered


def test_same_acronym_is_spoken_as_word() -> None:
    text = "This broadcast also carries SAME for selected locations."

    rendered = apply_voicetext_paul_vtml(text)

    assert '<vtml_sub alias="same">SAME</vtml_sub>' in rendered


def test_marine_units_and_direction_ranges_are_expanded() -> None:
    rendered = apply_voicetext_paul_vtml("W to SW winds 10 kt. Becoming W to SW. Seas 3 to 4 ft. Chance of tstms.")

    assert '<vtml_sub alias="west">W</vtml_sub>' in rendered
    assert '<vtml_sub alias="southwest">SW</vtml_sub>' in rendered
    assert '<vtml_sub alias="knots">kt</vtml_sub>' in rendered
    assert '<vtml_sub alias="feet">ft.</vtml_sub>' in rendered


def test_awips_national_paul_dictionary_fills_missing_rules() -> None:
    rendered = apply_voicetext_paul_vtml("The warning is near Johnsonville. Wind up to 20 kt.")

    assert '<vtml_phoneme alphabet="x-cmu" ph="JH AA0 N S AH0 N V IH0 L">Johnsonville</vtml_phoneme>' in rendered
    assert '<vtml_phoneme alphabet="x-cmu" ph="W IH1 N D">Wind</vtml_phoneme>' in rendered


def test_awips_national_paul_dictionary_does_not_override_existing_rules() -> None:
    rendered = apply_voicetext_paul_vtml("Fog remains possible.")

    assert rendered == 'Fog<vtml_pause time="0"/> remains possible.'


def test_awips_text_substitutions_do_not_leak_awips_markup_into_aliases() -> None:
    rendered = apply_voicetext_paul_vtml("Scattered frost is possible.")

    assert '<vtml_sub alias="scattered frost">Scattered frost</vtml_sub>' in rendered
    assert "<break" not in rendered
