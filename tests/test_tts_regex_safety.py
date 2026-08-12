from __future__ import annotations

import re

import pytest

from seasonalweather.tts.models import TextOverride
from seasonalweather.tts.preprocess import preprocess_text
from seasonalweather.tts.regex_safety import compile_safe_regex, validate_replacement
from seasonalweather.tts.voicetext_paul_vtml import apply_voicetext_paul_vtml


@pytest.mark.parametrize(
    "pattern",
    (
        r"literal",
        r"^forecast$",
        r"[ab]+[cd]+",
        r"(?:ab){1,3}",
        r"\x61+",
        r"\bNWS\b",
    ),
)
def test_safe_parser_and_python_re_agree_on_accepted_syntax(pattern: str) -> None:
    safe = compile_safe_regex(pattern)
    native = re.compile(pattern)
    for value in ("literal", "forecast", "aaaccd", "ababab", "aaa", "NWS"):
        assert bool(safe.search(value)) == bool(native.search(value))


@pytest.mark.parametrize(
    "pattern",
    (
        r"a{1,256}a{1,256}a{1,256}a{1,256}b",
        r"a{257,}",
        r"a{" + "9" * 100 + r",}",
        r"a{1,256}a+b",
        r"[ab]{1,256}[bc]{1,256}d",
        r"(?:a{1,256})(?:a{1,256})b",
        r"a*?",
        r"a+?",
        r"a{1,256}?",
        r"a*+",
        r"a++",
        r"a{1,256}+",
        r"a?a+",
        r"(a+)+",
        r"\N{LATIN SMALL LETTER A}",
        r"[\N{LATIN SMALL LETTER A}]",
        r"\q",
    ),
)
def test_unsafe_or_unmodeled_syntax_is_rejected_before_python_re(pattern: str, monkeypatch) -> None:
    entered = False

    def unexpected_compile(*_args, **_kwargs):
        nonlocal entered
        entered = True
        raise AssertionError("unsafe expression reached Python re")

    monkeypatch.setattr(re, "compile", unexpected_compile)
    with pytest.raises(ValueError):
        compile_safe_regex(pattern)
    assert not entered


def test_ignore_case_overlap_is_proved_conservatively() -> None:
    with pytest.raises(ValueError, match="competing"):
        compile_safe_regex(r"a+[A-Z]+", flags=re.IGNORECASE)


def test_sequential_ambiguous_alternation_is_rejected_before_python_re(monkeypatch) -> None:
    pattern = "".join("(?:a|aa)" for _ in range(25)) + "b"
    entered = False

    def unexpected_compile(*_args, **_kwargs):
        nonlocal entered
        entered = True
        raise AssertionError("ambiguous alternation reached Python re")

    monkeypatch.setattr(re, "compile", unexpected_compile)
    with pytest.raises(ValueError) as error:
        compile_safe_regex(pattern)
    assert "ambiguous competing alternatives" in str(error.value)
    assert not entered


@pytest.mark.parametrize("pattern", (r"(?:|a)", r"(?:a|)", r"(?:x|)" * 25 + "b"))
def test_nullable_alternation_is_rejected_before_python_re(pattern: str, monkeypatch) -> None:
    entered = False

    def unexpected_compile(*_args, **_kwargs):
        nonlocal entered
        entered = True
        raise AssertionError("nullable expression reached Python re")

    monkeypatch.setattr(re, "compile", unexpected_compile)
    with pytest.raises(ValueError) as error:
        compile_safe_regex(pattern)
    assert "nullable alternative" in str(error.value)
    assert not entered


def test_nested_nullable_alternation_and_ignore_case_are_rejected_structurally(monkeypatch) -> None:
    entered = False

    def unexpected_compile(*_args, **_kwargs):
        nonlocal entered
        entered = True
        raise AssertionError("nullable expression reached Python re")

    monkeypatch.setattr(re, "compile", unexpected_compile)
    with pytest.raises(ValueError) as error:
        compile_safe_regex(r"(?:(?:|a)|b)", flags=re.IGNORECASE)
    assert "nullable alternative" in str(error.value)
    assert not entered


@pytest.mark.parametrize("pattern", (r"(?:a|aa)", r"(?:(?:a|aa)|b)", r"(?:A|a)"))
def test_grouped_ambiguous_alternatives_are_rejected(pattern: str) -> None:
    flags = re.IGNORECASE if pattern == r"(?:A|a)" else 0
    with pytest.raises(ValueError, match="ambiguous competing alternatives"):
        compile_safe_regex(pattern, flags=flags)


def test_nested_ambiguous_alternation_is_rejected() -> None:
    with pytest.raises(ValueError, match="ambiguous competing alternatives"):
        compile_safe_regex(r"(?:x(?:a|aa)y|z)")


def test_disjoint_alternation_remains_supported() -> None:
    safe = compile_safe_regex(r"cat|dog")
    assert safe.fullmatch("cat")
    assert safe.fullmatch("dog")


def test_ignore_case_runtime_uses_the_same_safe_regex_flags() -> None:
    overrides = (TextOverride(match=r"NWS", replace="weather service", regex=True, ignore_case=True),)
    assert "weather service" in preprocess_text("nws", overrides)


@pytest.mark.parametrize("family", (("I", "i", "İ", "ı"), ("S", "s", "ſ"), ("K", "k", "K")))
def test_ignore_case_special_unicode_families_match_python_overlap(family: tuple[str, ...]) -> None:
    first, second = family[0], family[-1]
    with pytest.raises(ValueError, match="competing"):
        compile_safe_regex(f"[{first}]{{1,256}}[{second}]{{1,256}}b", flags=re.IGNORECASE)


def test_large_unicode_class_range_is_rejected_before_materialization() -> None:
    with pytest.raises(ValueError, match="character range"):
        compile_safe_regex(r"[\u0000-\uffff]")


def test_small_character_class_ranges_remain_supported() -> None:
    safe = compile_safe_regex(r"[a-f]+")
    assert safe.fullmatch("abcdef")


@pytest.mark.parametrize(
    "replacement",
    [r"\1", r"\2", r"\3", r"\4", r"\5", r"\6", r"\7", r"\8", r"\9", r"\g<name>", r"\k<name>"],
)
def test_replacement_backreferences_are_rejected_by_the_shared_authority(replacement: str) -> None:
    with pytest.raises(ValueError):
        validate_replacement(replacement)


def test_voicetext_configured_rules_use_the_shared_authority() -> None:
    rendered = apply_voicetext_paul_vtml(
        "NWS",
        vtml_lexicon=False,
        alias_overrides=[{"match": r"NWS", "alias": "weather service", "regex": True}],
    )
    assert 'alias="weather service"' in rendered

    with pytest.raises(ValueError):
        apply_voicetext_paul_vtml(
            "a" * 65_000,
            vtml_lexicon=False,
            alias_overrides=[{"match": r"(a+)+$", "alias": "unsafe", "regex": True}],
        )


def test_voicetext_configured_rule_count_and_replacement_work_are_bounded() -> None:
    rules = [{"match": str(index), "alias": "x"} for index in range(33)]
    with pytest.raises(ValueError, match="too many"):
        apply_voicetext_paul_vtml("text", vtml_lexicon=False, alias_overrides=rules)

    with pytest.raises(ValueError, match="replacement work"):
        apply_voicetext_paul_vtml(
            "a" * 9_000,
            vtml_lexicon=False,
            alias_overrides=[{"match": "a", "alias": "x"}],
        )
