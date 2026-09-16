"""How a message's roleplay markup reads: its two axes, and its narration."""

from backend.analysis.text.markup import classify_axes, narration_only
from backend.analysis.text.roleplay import AxisStyle, Dialogue, Narration

QUOTES_ONLY = 'She smiles and steps back. "I won\'t go," she says, turning to the window.'
ASTERISKS_ONLY = "*She smiles and steps back, turning to the window.* I won't go."
FULL_MARKUP = '*He leans on the doorframe, arms crossed.* "You came back," *he murmurs.*'


# ---------- the two axes ----------


def test_classify_quotes_only():
    style = classify_axes(QUOTES_ONLY)
    assert style.dialogue == Dialogue.QUOTED
    assert style.narration == Narration.BARE


def test_classify_asterisks_only():
    style = classify_axes(ASTERISKS_ONLY)
    assert style.dialogue == Dialogue.BARE
    assert style.narration == Narration.ASTERISK


def test_classify_full_markup():
    style = classify_axes(FULL_MARKUP)
    assert style.dialogue == Dialogue.QUOTED
    assert style.narration == Narration.ASTERISK


def test_pure_dialogue_has_unknown_narration():
    style = classify_axes('"Hello." "How are you?" "Fine, thanks."')
    assert style.dialogue == Dialogue.QUOTED
    assert style.narration == Narration.UNKNOWN


def test_embedded_thought_does_not_flip_narration_axis():
    text = (
        "She paused at the door, one hand on the frame. "
        "*Was he really serious about this?* "
        '"Tell me the truth," she said quietly.'
    )
    assert classify_axes(text).narration != Narration.ASTERISK


# ---------- formatting that is not RP markup ----------


def test_markdown_bullets_not_treated_as_emphasis():
    text = "Here is a list:\n* first item\n* second item\nThat is all."
    assert classify_axes(text).narration != Narration.ASTERISK


def test_asterisk_inside_quotes_is_not_narration():
    text = 'He said, "you are *so* dramatic," and rolled his eyes as she huffed.'
    style = classify_axes(text)
    assert style.dialogue == Dialogue.QUOTED


def test_code_block_markup_does_not_sway_classification():
    text = "She nods.\n\n```\n*this is code* and ***bold*** stuff\n```\n\nShe leaves."
    style = classify_axes(text)
    assert style.narration != Narration.ASTERISK


def test_emphasis_in_dialogue_not_misread_as_narration_axis():
    text = '"You are *so* dramatic," he said, "and *always* late."'
    style = classify_axes(text)
    assert style.dialogue == Dialogue.QUOTED
    assert style.narration != Narration.ASTERISK


# ---------- the narration dead band ----------

HALF_ASTERISKED_DRAFT = (
    "Amaryllis blinked, caught off guard by how serious the question was.\n\n"
    "Her fingers tightened around the spine of the book.\n\n"
    "*She let out a short, breathy laugh, the first in weeks.*\n\n"
    '"Battle scenes?" *she asked, her accent lilting.* "Aye, there\'s a few."\n\n'
    '*She cleared her throat and looked away.* "There\'s a siege in the third act."'
)


def test_half_asterisked_draft_classifies_into_the_dead_band():
    assert classify_axes(HALF_ASTERISKED_DRAFT).narration == Narration.UNKNOWN


# ---------- emphasis that is not an action beat ----------


def test_an_attributed_line_in_the_bare_runs_vetoes_the_bare_dialogue_read():
    """An attributed line marks its surrounding run as narration."""
    draft = "I set the cup down. *This was going badly.* Fine, I said, and looked away."
    assert classify_axes(draft).dialogue == Dialogue.UNKNOWN


def test_mid_sentence_italics_are_still_not_action_beats():
    """Widening the terminator set must not promote inline emphasis."""
    for draft in (
        "She was *really* nervous about the whole thing.",
        "I told you it was *his* fault, not mine.",
        "It had been *years* since anyone said her name like that.",
    ):
        assert classify_axes(draft).narration != Narration.ASTERISK, draft


# ---------- first-person action beats ----------

FIRST_PERSON_BEAT = (
    "*I smile kindly at you.* Hello, Kai. Thank you for coming to our club. "
    "As president of the Literature Club, it's my duty to make the club fun and "
    "exciting for everyone! Tell me, what brings you here today?"
)
THIRD_PERSON_BEAT = FIRST_PERSON_BEAT.replace("*I smile kindly at you.*", "*Monika smiles kindly at you.*")


def test_a_first_person_action_beat_classifies_like_its_third_person_twin():
    assert classify_axes(FIRST_PERSON_BEAT) == classify_axes(THIRD_PERSON_BEAT)
    assert classify_axes(FIRST_PERSON_BEAT) == AxisStyle(Dialogue.BARE, Narration.ASTERISK)


def test_a_beat_with_a_first_person_object_is_still_an_action_beat():
    draft = "*She reaches for me.* Hello there. Are you sure you want to stay here tonight?"
    assert classify_axes(draft) == AxisStyle(Dialogue.BARE, Narration.ASTERISK)


# ---------- quotation edge cases ----------


def test_smart_quotes_classified_as_quoted_dialogue():
    text = "She smiles and steps back. “I won’t go,” she says."
    style = classify_axes(text)
    assert style.dialogue == Dialogue.QUOTED


# ---------- narration extraction ----------


def test_narration_only_obeys_the_resolved_dialogue_convention():
    """Equivalent scenes expose narration rather than whichever spans have quotes."""
    bare = (
        "As president of the Literature Club, it's my duty to make the club fun and exciting for everyone! "
        "*Monika smiles kindly at you.* Tell me, what brings you here today?"
    )
    quoted = (
        '"As president of the Literature Club, it\'s my duty to make the club fun and exciting for everyone!" '
        'Monika smiles kindly at you. She waits by the desk. "Tell me, what brings you here today?"'
    )

    assert narration_only(bare, Dialogue.BARE) == "Monika smiles kindly at you."
    assert narration_only(quoted, Dialogue.QUOTED) == "Monika smiles kindly at you. She waits by the desk."
