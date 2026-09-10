"""Narration supplied to the POV/tense classifier."""

import pytest

from backend.analysis.format_consistency import Dialogue, classify_axes, narration_only


@pytest.mark.parametrize("marker", ["*", "_"])
@pytest.mark.parametrize(
    "tag",
    [
        "she thinks with a frown.",
        "Mara thought, unable to shake the unease.",
        "The guard tells himself, reaching for the latch.",
        "I remind myself quietly.",
    ],
)
def test_explicit_thought_attribution_keeps_the_narrator(marker, tag):
    text = f"{marker}You can do this. You have to keep moving.{marker} {tag} The door creaks."
    assert narration_only(text, Dialogue.BARE) == f"{tag} The door creaks."


def test_only_the_attributed_thought_is_removed():
    text = "*I approached her.* *Stay calm,* she tells herself, reaching for the latch."
    assert narration_only(text, Dialogue.BARE) == "*I approached her.* she tells herself, reaching for the latch."


def test_thought_attribution_does_not_admit_another_paragraphs_bare_speech():
    text = "*Stay calm,* she tells herself.\n\n*She waits by the door.* I tell you."
    assert narration_only(text, Dialogue.BARE) == "she tells herself. She waits by the door."


@pytest.mark.parametrize(
    "speech",
    ["I think you need to rest.", "She thought you knew.", "He thinks I should leave.", "They think the bridge is safe."],
)
def test_a_thought_verb_with_a_clause_is_not_an_attribution(speech):
    text = f"*Mara hesitated by the doorway.* {speech}"
    assert narration_only(text, Dialogue.BARE) == "Mara hesitated by the doorway."


def test_a_thought_tag_inside_quoted_speech_cannot_affect_extraction():
    text = 'Mara hesitated. "*Stay calm,* she thinks, or so she says."'
    assert narration_only(text, classify_axes(text).dialogue) == "Mara hesitated."


def test_removing_a_quote_does_not_attach_its_thought_tag_to_an_action():
    text = '*Mara sat.* "I should go," she thinks.'
    assert narration_only(text, Dialogue.QUOTED) == "*Mara sat.* she thinks."


def test_inline_emphasis_before_a_thought_verb_remains_narration():
    text = "About the _door_ she thinks, while waiting for the guard."
    assert narration_only(text, Dialogue.UNKNOWN) == "About the *door* she thinks, while waiting for the guard."


def test_retained_underscore_emphasis_uses_the_models_asterisk_convention():
    text = "_I hesitate by the doorway._ He thinks I should leave."
    assert narration_only(text, classify_axes(text).dialogue) == "*I hesitate by the doorway.* He thinks I should leave."


@pytest.mark.parametrize(
    "text",
    [
        "She reads the snake_case identifier.",
        r"She reads \_literal text\_.",
        "She holds __both__ doors open.",
        "ヽ(⌐■_■)ノ Hello there ヽ(⌐■_■)ノ",
    ],
)
def test_canonicalization_leaves_non_emphasis_underscores_alone(text):
    assert narration_only(text, Dialogue.UNKNOWN) == text


def test_quotes_spanning_paragraphs_are_removed_as_one_span():
    text = 'She waits. "You need to rest.\n\nI will keep watch." She nods.'
    assert narration_only(text, Dialogue.QUOTED) == "She waits. She nods."
