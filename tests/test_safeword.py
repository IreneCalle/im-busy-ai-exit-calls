"""Safe word matching: what fires, what must not, and what the model is shown."""

from __future__ import annotations

import pytest

from imbusy.safety.safeword import (
    SafeWordError,
    SafeWordMatcher,
    edit_distance,
    normalise,
    parse_safe_word,
)

PHRASE = parse_safe_word("Pink flamingo")


def test_normalise_drops_case_accents_and_punctuation() -> None:
    assert normalise("¡Flamenco, ROSA! ¿Vale?") == ["flamenco", "rosa", "vale"]
    assert normalise("Ñandú-Azul") == ["nandu", "azul"]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("flamingo", "too_few_words"),
        ("ok go", "too_short"),
        ("one two three four five", "too_many_words"),
        ("a " + "b" * 70, "too_long"),
    ],
)
def test_unusable_phrases_are_rejected_with_a_reason(raw: str, reason: str) -> None:
    with pytest.raises(SafeWordError) as info:
        parse_safe_word(raw)
    assert info.value.reason == reason


def test_phrase_is_stored_normalised() -> None:
    assert parse_safe_word("  Flamenco   ROSA ") == "flamenco rosa"


@pytest.mark.parametrize(
    "said",
    [
        "pink flamingo",
        "Pink Flamingo!",
        "yeah, so, pink flamingo, anyway",
        "pink flamingos",  # plural: one extra letter
        "pinc flamingo",  # one wrong letter
        "pink flaming go",  # a word split in two by speech-to-text
    ],
)
def test_fires_on_the_phrase_and_on_small_transcription_slips(said: str) -> None:
    assert SafeWordMatcher(PHRASE).heard(said)


@pytest.mark.parametrize(
    "said",
    [
        "pink flamenco",  # two letters off: a different word
        "I love flamingos, they are pink",  # both words, wrong order and not together
        "flamingo",  # half the phrase
        "think flamingo",  # two edits away
        "",
    ],
)
def test_does_not_fire_on_near_misses(said: str) -> None:
    assert not SafeWordMatcher(PHRASE).heard(said)


def test_spanish_phrase_with_accents_said_without_them() -> None:
    matcher = SafeWordMatcher(parse_safe_word("Canción del verano"))
    assert matcher.heard("oye, cancion del verano")
    assert not matcher.heard("la canción de este verano")


def test_long_phrases_allow_two_slips() -> None:
    matcher = SafeWordMatcher(parse_safe_word("blue umbrella sandwich"))  # 19 letters
    assert matcher.heard("blue umbrela sanwich")
    assert not matcher.heard("blue umbrella")


def test_edit_distance_stops_early_past_the_limit() -> None:
    assert edit_distance("flamingo", "flamenco", limit=1) == 2
    assert edit_distance("abc", "abcdefgh", limit=1) == 2


def test_redact_removes_the_phrase_for_the_model() -> None:
    matcher = SafeWordMatcher(PHRASE)
    assert matcher.redact("vale, pink flamingo, voy") == "vale voy"
    assert matcher.redact("Pink flamingo.") == "[inaudible]"
    assert matcher.redact("nada que ver") == "nada que ver"
