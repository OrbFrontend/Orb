"""Tests for the RP format-consistency normalizer."""

from backend.analysis.format_consistency import (
    AxisStyle,
    Dialogue,
    Narration,
    baseline_axes,
    classify_axes,
    narration_only,
    normalize_format,
    normalize_to_baseline,
    skip_reasons,
    stable_label,
    vote_axes,
)

QUOTES_ONLY = 'She smiles and steps back. "I won\'t go," she says, turning to the window.'
ASTERISKS_ONLY = "*She smiles and steps back, turning to the window.* I won't go."
FULL_MARKUP = '*He leans on the doorframe, arms crossed.* "You came back," *he murmurs.*'


def _assert_unchanged(draft: str, base: list[str], *, enabled: bool = True):
    new, report = normalize_to_baseline(draft, base, enabled=enabled)
    assert not report.changed
    assert new == draft
    return report


# ---------- classification ----------


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


# ---------- the reported inversion ----------


def test_inversion_asterisk_draft_to_quotes_baseline():
    base = ['She smiles. "Hello there," she says warmly.']
    draft = "*She steps closer, watching him carefully.* Are you sure about this?"
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert new == 'She steps closer, watching him carefully. "Are you sure about this?"'


def test_inversion_quotes_draft_to_asterisk_baseline():
    base = ["*She smiles, stepping back toward the window.* Hello there."]
    draft = 'She steps closer, watching him. "Are you sure about this?"'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert new == "*She steps closer, watching him.* Are you sure about this?"


# ---------- per-axis: only the drifted axis is touched ----------


def test_only_narration_axis_changes_quotes_preserved():
    base = ['*He leans against the doorframe, arms crossed.* "You came back," *he murmurs.*']
    draft = 'He leans against the wall. "You came back."'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert new == '*He leans against the wall.* "You came back."'


def test_full_markup_to_quotes_only_strips_narration_asterisks():
    target = AxisStyle(dialogue=Dialogue.QUOTED, narration=Narration.BARE)
    out = normalize_format(FULL_MARKUP, target)
    assert "*" not in out.replace("*he murmurs.*", "")  # block narration unwrapped
    assert '"You came back,"' in out  # dialogue untouched


QUOTES_BASELINE = [
    'She smiles. "Hello there," she says warmly.',
    'He nods. "Welcome back," he replies.',
]


# ---------- no-op safety ----------


def test_already_consistent_is_byte_identical():
    base = ['She smiles. "Hello there," she says warmly.']
    draft = 'He nods slowly. "I understand," he replies.'
    _assert_unchanged(draft, base)


def test_disabled_is_noop():
    base = ["*She smiles.* Hello there."]
    draft = 'She smiles. "Hello there."'
    rep = _assert_unchanged(draft, base, enabled=False)
    assert rep.note == "disabled"


def test_no_baseline_is_noop():
    draft = 'She smiles. "Hello there."'
    _assert_unchanged(draft, [])


def test_unstable_baseline_is_noop():
    base = [QUOTES_ONLY, ASTERISKS_ONLY]
    target = baseline_axes(base)
    assert target.dialogue == Dialogue.UNKNOWN
    assert target.narration == Narration.UNKNOWN
    draft = 'She frowns. "What now?"'
    _assert_unchanged(draft, base)


# ---------- preservation of incidental markup ----------


def test_contractions_survive():
    base = ["*She waves.* Hi there."]
    draft = "She can't believe it. \"I won't leave,\" she insists."
    new, _ = normalize_to_baseline(draft, base, enabled=True)
    assert "can't" in new
    assert "won't" in new


def test_markdown_bullets_not_treated_as_emphasis():
    text = "Here is a list:\n* first item\n* second item\nThat is all."
    assert classify_axes(text).narration != Narration.ASTERISK


def test_asterisk_inside_quotes_is_not_narration():
    text = 'He said, "you are *so* dramatic," and rolled his eyes as she huffed.'
    style = classify_axes(text)
    assert style.dialogue == Dialogue.QUOTED


# ---------- protected asterisk runs (markdown bold-italic / scene dividers) ----------


def test_bold_italic_run_preserved_while_prose_normalizes():
    base = QUOTES_BASELINE
    draft = '*He leans in close.* "You came back." ***He could not believe it.***'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert new == 'He leans in close. "You came back." ***He could not believe it.***'


def test_scene_divider_run_preserved_with_surrounding_text():
    base = QUOTES_BASELINE
    draft = "She turns away.\n\n***\n\nThe room falls silent."
    _assert_unchanged(draft, base)  # divider and its blank-line spacing untouched


# ---------- fenced code blocks (literal content, never reformatted) ----------


def test_code_block_markup_does_not_sway_classification():
    text = "She nods.\n\n```\n*this is code* and ***bold*** stuff\n```\n\nShe leaves."
    style = classify_axes(text)
    assert style.narration != Narration.ASTERISK


def test_code_block_passes_through_rewrite_verbatim():
    base = QUOTES_BASELINE
    draft = '*He leans in.* "You came back."\n\n```python\nx = a ***b*** c  # not RP markup\n```'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert new == 'He leans in. "You came back."\n\n```python\nx = a ***b*** c  # not RP markup\n```'


# ---------- *emphasis* inside dialogue (LLMs do this constantly) ----------


def test_emphasis_in_dialogue_survives_narration_strip():
    base = ['She smiles. "Hello there."']
    draft = '*He leans in.* "Do you think I am *stupid*?"'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert new == 'He leans in. "Do you think I am *stupid*?"'
    assert "*stupid*" in new
    assert new.count("*") == 2


def test_emphasis_in_dialogue_not_misread_as_narration_axis():
    text = '"You are *so* dramatic," he said, "and *always* late."'
    style = classify_axes(text)
    assert style.dialogue == Dialogue.QUOTED
    assert style.narration != Narration.ASTERISK


def test_emphasis_survives_dialogue_flattening_against_asterisk_baseline():
    base = [
        "*She smiles, stepping back.* Hello there.",
        "*He follows her in.* Good to see you.",
    ]
    draft = 'He frowns. "Do you think I am *stupid*?"'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert "*stupid*" in new
    assert "stupid" in new


def test_multiparagraph_preserves_separators():
    base = ['*She nods.* "Okay."']  # full-ish / asterisk narration baseline
    draft = "She nods slowly.\n\nShe steps away from the table."
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert "\n\n" in new
    if rep.changed:
        assert new.count("\n\n") == draft.count("\n\n")


def test_inline_emphasis_inside_narration_not_fragmented():
    base = ['*He waited by the window, tense.* "Where were you?"']
    draft = '"Where is he?" She was *really* nervous about the whole thing.'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert new == '"Where is he?" *She was really nervous about the whole thing.*'
    assert "*really*" not in new


def test_pure_bare_narration_without_dialogue_is_noop():
    base = ["*She paces the room nervously, glancing at the clock.* Right."]
    draft = "She was really nervous about the whole thing."
    _assert_unchanged(draft, base)


# ---------- multi-turn baselines (the cross-message regression) ----------


def test_stable_multiturn_quotes_baseline_converts_asterisk_turn():
    base = [
        'She smiles. "Hello there," she says warmly.',
        'He nods. "Good to see you again," he replies.',
        'She laughs softly. "It has been too long."',
    ]
    target = baseline_axes(base)
    assert target.dialogue == Dialogue.QUOTED
    assert target.narration == Narration.BARE
    draft = "*He leans against the doorframe, studying her.* You look tired."
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert "*" not in new


def test_stable_multiturn_asterisk_baseline_converts_quotes_turn():
    base = [
        "*She smiles, stepping back toward the window.* Hello there.",
        "*He follows, hands in his pockets.* Good to see you.",
        "*She turns to face him fully.* It has been too long.",
    ]
    target = baseline_axes(base)
    assert target.dialogue == Dialogue.BARE
    assert target.narration == Narration.ASTERISK
    draft = 'He leans against the doorframe. "You look tired," he says.'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert '"' not in new
    assert "*" in new


def test_drift_in_only_the_latest_turn_does_not_change_a_consistent_draft():
    base = [
        'She smiles. "Hello there."',
        'He nods. "Welcome back."',
    ]
    draft = 'She tilts her head. "What brings you here?"'
    _assert_unchanged(draft, base)


# ---------- within-message drift ----------

FULL_MARKUP_BASELINE = [
    '*He leaned against the bookshelf, watching her.* "You came back," *he murmured.*',
    '*She set down the book, fingers trembling.* "I told you I would," *she said softly.*',
    '*He crossed the room in three steps.* "Then prove it," *he whispered.*',
]


def test_stray_bare_dialogue_tag_is_wrapped_when_dominant_style_matches():
    draft = (
        "*She pulled the curtain aside and glanced out at the street.*\n\n"
        '"It looks like it might rain," she replied.\n\n'
        "*She let the fabric fall back and turned to the window latch.*"
    )
    new, rep = normalize_to_baseline(draft, FULL_MARKUP_BASELINE, enabled=True)
    assert rep.changed
    assert '"It looks like it might rain," *she replied.*' in new
    assert "*She pulled the curtain aside and glanced out at the street.*" in new
    assert "*She let the fabric fall back and turned to the window latch.*" in new


def test_quoteless_full_markup_draft_wraps_bare_paragraph_in_asterisks_not_quotes():
    draft = (
        "*Jane traced a finger along the spines of the books on the shelf.*\n\n"
        "The librarian tilted her head, her blue eyes scanning the titles.\n\n"
        "*She slid one volume free and weighed it in her hands.*"
    )
    new, rep = normalize_to_baseline(draft, FULL_MARKUP_BASELINE, enabled=True)
    assert rep.changed
    assert '"' not in new
    assert "*The librarian tilted her head, her blue eyes scanning the titles.*" in new


def test_genuine_asterisk_convention_draft_is_not_misread_as_full_markup():
    base = [
        "*She smiles, stepping back.* Hello there.",
        "*He follows her in.* Good to see you.",
        "*She turns to face him.* It has been too long.",
    ]
    draft = "*He shifts his weight.*\n\nAre you sure about this?\n\n*She waits.*"
    _assert_unchanged(draft, base)


# ---------- a draft split between the two narration conventions ----------

HALF_ASTERISKED_DRAFT = (
    "Amaryllis blinked, caught off guard by how serious the question was.\n\n"
    "Her fingers tightened around the spine of the book.\n\n"
    "*She let out a short, breathy laugh, the first in weeks.*\n\n"
    '"Battle scenes?" *she asked, her accent lilting.* "Aye, there\'s a few."\n\n'
    '*She cleared her throat and looked away.* "There\'s a siege in the third act."'
)


def test_half_asterisked_draft_classifies_into_the_dead_band():
    assert classify_axes(HALF_ASTERISKED_DRAFT).narration == Narration.UNKNOWN


BARE_NARRATION_BASELINE = [
    'She set the book down. "I told you I would," she said softly.',
    'He crossed the room in three steps. "Then prove it," he whispered.',
    'She looked away. "It doesnae matter," she muttered.',
]


def test_half_asterisked_draft_is_completed_against_a_bare_narration_baseline():
    new, rep = normalize_to_baseline(HALF_ASTERISKED_DRAFT, BARE_NARRATION_BASELINE, enabled=True)
    assert rep.changed
    assert new == (
        "Amaryllis blinked, caught off guard by how serious the question was.\n\n"
        "Her fingers tightened around the spine of the book.\n\n"
        "She let out a short, breathy laugh, the first in weeks.\n\n"
        '"Battle scenes?" she asked, her accent lilting. "Aye, there\'s a few."\n\n'
        'She cleared her throat and looked away. "There\'s a siege in the third act."'
    )


# A bare-narration chat that sets its thoughts in asterisks.
THOUGHT_IN_ASTERISKS_BASELINE = [
    'She set the book down. *He never listens.* "I told you I would," she said softly.',
    *BARE_NARRATION_BASELINE[1:],
]


def test_half_asterisked_draft_is_left_alone_where_the_chat_writes_block_emphasis():
    assert baseline_axes(THOUGHT_IN_ASTERISKS_BASELINE).narration == Narration.BARE
    _assert_unchanged(HALF_ASTERISKED_DRAFT, THOUGHT_IN_ASTERISKS_BASELINE)


def test_half_asterisked_draft_is_completed_against_an_asterisk_baseline():
    new, rep = normalize_to_baseline(HALF_ASTERISKED_DRAFT, FULL_MARKUP_BASELINE, enabled=True)
    assert rep.changed
    assert "*Amaryllis blinked, caught off guard by how serious the question was.*" in new
    assert "*She let out a short, breathy laugh, the first in weeks.*" in new
    assert '"Battle scenes?"' in new


# ---------- punctuation / glyph preservation across a rewrite ----------


def test_question_and_exclamation_preserved_through_inversion():
    base = ["*She smiles, stepping back.* Hello there."]
    draft = 'She gasps. "Is that really you?! I cannot believe it!"'
    new, _ = normalize_to_baseline(draft, base, enabled=True)
    assert "?!" in new
    assert new.endswith("!")


# ---------- mixed-format draft against a single-axis baseline ----------


def test_mixed_draft_against_quotes_baseline_strips_only_narration_asterisks():
    base = [
        'She smiles. "Hello there."',
        'He nods. "Welcome back."',
    ]
    draft = '*He steps inside, shaking off the rain.* "Quite a storm out there," he says.'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert "*" not in new
    assert '"Quite a storm out there,"' in new


def test_mixed_draft_against_asterisk_baseline_strips_only_dialogue_quotes():
    base = [
        "*She smiles, stepping back.* Hello there.",
        "*He follows her in.* Welcome back.",
    ]
    draft = '*He steps inside, shaking off the rain.* "Quite a storm out there," he says.'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert '"' not in new
    assert "*He steps inside, shaking off the rain.*" in new


# ---------- multiple dialogue beats in one turn ----------


def test_asterisk_narration_without_quotes_reads_as_bare_dialogue():
    base = ['She smiles. "Hello there."']
    draft = "*She steps closer.* Are you sure? *She hesitates.* Really sure?"
    assert classify_axes(draft).dialogue == Dialogue.BARE
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert new == 'She steps closer. "Are you sure?" She hesitates. "Really sure?"'


def test_italic_thoughts_in_prose_are_not_read_as_bare_dialogue():
    draft = (
        "Sayori's phone slips from her numb fingers.\n\n"
        "*He still likes me. He really does.*\n\n"
        "The thought is not a comfort but an accusation."
    )
    style = classify_axes(draft)
    assert style.dialogue == Dialogue.UNKNOWN
    assert style.narration == Narration.UNKNOWN
    _assert_unchanged(draft, ['She smiles. "Hello there."'])


def test_the_same_passage_classifies_the_same_in_first_and_third_person():
    """Person and convention are independent."""
    thought = "*He still likes me. He really does.*"
    third = f"Sayori's phone slips from her numb fingers.\n\n{thought}\n\nThe thought is not a comfort."
    first = f"My phone slips from my numb fingers.\n\n{thought}\n\nThe thought is not a comfort."

    assert classify_axes(third) == classify_axes(first)
    assert classify_axes(first).dialogue == Dialogue.UNKNOWN
    for draft in (third, first):
        _assert_unchanged(draft, ['She smiles. "Hello there."'])


def test_an_italic_aside_written_from_outside_is_still_not_bare_dialogue():
    """Third-person bare runs veto the bare-dialogue reading: nothing is quoted. The
    aside itself is stray narration in a chat that never writes block emphasis."""
    draft = "She walked to the window. *Everything had changed.* The street below was empty."
    assert classify_axes(draft).dialogue == Dialogue.UNKNOWN
    new, _ = normalize_to_baseline(draft, ['She smiles. "Hello there."'], enabled=True)
    assert new == "She walked to the window. Everything had changed. The street below was empty."


def test_an_attributed_line_in_the_bare_runs_vetoes_the_bare_dialogue_read():
    """An attributed line marks its surrounding run as narration."""
    draft = "I set the cup down. *This was going badly.* Fine, I said, and looked away."
    assert classify_axes(draft).dialogue == Dialogue.UNKNOWN


def test_a_talkative_bare_dialogue_baseline_still_sets_the_axes():
    greeting = (
        "Hello, Kai. Thank you for coming to our club. As president of the Literature "
        "Club, it's my duty to make the club fun and exciting for everyone! "
        "*Monika smiles kindly at you.* Tell me, what brings you here today?"
    )
    assert baseline_axes([greeting]) == AxisStyle(Dialogue.BARE, Narration.ASTERISK)

    draft = (
        "Monika's smile is still perfectly in place.\n\n"
        '"Ah... Sayori."\n\n'
        "She laughs quietly and airily.\n\n"
        "\"But now that you're here, let's focus on you.\""
    )
    new, rep = normalize_to_baseline(draft, [greeting], enabled=True)
    assert rep.changed
    assert '"' not in new
    assert "*Monika's smile is still perfectly in place.*" in new
    assert "Ah... Sayori." in new


def test_a_greeting_that_ends_on_stylistic_punctuation_still_sets_the_axes():
    """A "~" closes a clause, so the beat after it is narration, not italics."""
    for greeting in (
        "miku dayo~ *does a weird dance*",
        "Nyaa\u266a *pounces on the keyboard*",
        "Tch\u2014 *crosses arms*",
        "hi (^_^) *bounces on her heels*",
    ):
        assert baseline_axes([greeting]) == AxisStyle(Dialogue.BARE, Narration.ASTERISK), greeting


def test_an_asterisk_greeting_does_not_strip_the_next_reply():
    """The regression: a bare-narration baseline unwrapped every action beat."""
    greeting = "miku dayo~ *does a weird dance*"
    draft = "*stares at you with wide, vacant eyes.*\n\n*aggressive leek spinning*"
    _assert_unchanged(draft, [greeting])


def test_mid_sentence_italics_are_still_not_action_beats():
    """Widening the terminator set must not promote inline emphasis."""
    for draft in (
        "She was *really* nervous about the whole thing.",
        "I told you it was *his* fault, not mine.",
        "It had been *years* since anyone said her name like that.",
    ):
        assert classify_axes(draft).narration != Narration.ASTERISK, draft


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


def test_a_window_that_votes_bare_on_both_axes_enforces_nothing():
    base = [
        "*She smiles and steps back.* I won't go, and you cannot make me.",
        "The rain kept on against the glass, steady and grey, long after she had gone.",
        "He waited by the door for a while, then gave up and went back inside.",
    ]
    assert baseline_axes(base) == AxisStyle(Dialogue.BARE, Narration.BARE)
    _assert_unchanged('*He leans in.* "You came back," he says.', base)


# ---------- classification edge cases ----------


def test_smart_quotes_classified_as_quoted_dialogue():
    text = "She smiles and steps back. “I won’t go,” she says."
    style = classify_axes(text)
    assert style.dialogue == Dialogue.QUOTED


def test_single_word_message_is_ambiguous_noop():
    base = ['She smiles. "Hello there."']
    draft = "Okay."
    _assert_unchanged(draft, base)


def test_empty_draft_is_noop():
    base = ['She smiles. "Hello there."']
    _assert_unchanged("", base)


# ---------- the agreement rule, on its own ----------


def test_stable_label_trusts_a_single_confident_sample():
    assert stable_label(["third"], "ambiguous") == "third"


def test_stable_label_ignores_the_unknown_sentinel():
    assert stable_label(["ambiguous", "past", "ambiguous"], "ambiguous") == "past"


def test_stable_label_is_unknown_when_nothing_is_confident():
    assert stable_label(["ambiguous", "ambiguous"], "ambiguous") == "ambiguous"
    assert stable_label([], "ambiguous") == "ambiguous"


def test_stable_label_needs_a_60_percent_majority():
    assert stable_label(["third", "third", "first"], "ambiguous") == "third"
    assert stable_label(["third", "third", "first", "second"], "ambiguous") == "ambiguous"


def test_stable_label_needs_two_occurrences_not_just_a_plurality():
    assert stable_label(["third", "first", "second"], "ambiguous") == "ambiguous"


def test_stable_label_rejects_an_even_split():
    assert stable_label(["past", "present"], "ambiguous") == "ambiguous"


# ---------- stray narration in a bare or mixed draft ----------

THOUGHT_IN_QUOTED_PROSE = 'She crossed the room slowly. *He still loves me. He has to.* "Good night," she said.'


def test_an_off_voice_thought_survives_a_third_person_chat():
    """First person in a chat whose narration never speaks in it is a thought."""
    style = classify_axes(THOUGHT_IN_QUOTED_PROSE)
    assert style.dialogue == Dialogue.QUOTED
    assert style.narration == Narration.UNKNOWN

    _assert_unchanged(THOUGHT_IN_QUOTED_PROSE, BARE_NARRATION_BASELINE)


def test_a_first_person_thought_is_unwrapped_where_the_chat_writes_its_thoughts_bare():
    base = [*BARE_NARRATION_BASELINE[:2], 'She looked away. Why do I even bother? "It doesnae matter," she muttered.']
    new, rep = normalize_to_baseline(THOUGHT_IN_QUOTED_PROSE, base, enabled=True)
    assert rep.changed
    assert new == 'She crossed the room slowly. He still loves me. He has to. "Good night," she said.'


STRAY_BEAT_DRAFT = (
    "She crossed the room slowly and sat down on the edge of the old bed, smoothing the blanket "
    'flat with both hands. *He was lying again.* "Fine," she said.'
)


def test_a_bare_draft_loses_its_stray_narration():
    """The intra-reply slip: the chat's bare prose with one span left in asterisks."""
    assert classify_axes(STRAY_BEAT_DRAFT).narration == Narration.BARE
    new, rep = normalize_to_baseline(STRAY_BEAT_DRAFT, BARE_NARRATION_BASELINE, enabled=True)
    assert rep.changed
    assert new == STRAY_BEAT_DRAFT.replace("*He was lying again.*", "He was lying again.")


def test_a_bare_draft_keeps_its_emphasis_where_the_chat_writes_block_emphasis():
    _assert_unchanged(STRAY_BEAT_DRAFT, THOUGHT_IN_ASTERISKS_BASELINE)


def test_a_bare_draft_keeps_its_sound_effects_and_stage_directions():
    """A lone word is a sound effect, and a lower-case beat has no subject once unwrapped."""
    for draft in (
        "The door slammed behind him hard enough to rattle the frames on the wall. *thud* "
        '"Charming," she muttered, and turned the page of her book without looking up.',
        'She stood by the window for a long while, watching the rain run down the glass. *sighs softly* "Fine," she said.',
    ):
        assert classify_axes(draft).narration == Narration.BARE
        _assert_unchanged(draft, BARE_NARRATION_BASELINE)


def test_a_settled_bare_narration_draft_is_still_stripped():
    draft = '*He steps inside, shaking off the rain.* "Quite a storm out there," he says.'
    assert classify_axes(draft).narration == Narration.ASTERISK
    new, rep = normalize_to_baseline(draft, BARE_NARRATION_BASELINE, enabled=True)
    assert rep.changed
    assert "*" not in new


def test_wrapping_bare_narration_does_not_swallow_a_marked_span():
    draft = 'She walked. *He still loves me.* She stopped. "Hello," she said.'
    new, rep = normalize_to_baseline(draft, FULL_MARKUP_BASELINE, enabled=True)
    assert rep.changed
    assert new == '*She walked.* *He still loves me.* *She stopped.* "Hello," *she said.*'


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


def test_a_first_person_convention_normalizes_a_quoted_reply():
    draft = 'I lean against the desk. "Take a seat, then," I say.'
    new, rep = normalize_to_baseline(draft, [FIRST_PERSON_BEAT], enabled=True)
    assert rep.changed
    assert '"' not in new
    assert "*I lean against the desk.*" in new


def test_an_interior_beat_is_still_not_an_action_beat_in_first_person():
    draft = "My phone slips from my numb fingers.\n\n*He still likes me. He really does.*\n\nThe thought is not a comfort."
    assert classify_axes(draft).dialogue == Dialogue.UNKNOWN
    _assert_unchanged(draft, ['She smiles. "Hello there."'])


# ---------- an explicit source classification ----------


def test_an_explicit_source_replaces_the_draft_classification():
    """Another classifier can decide the source side of the rewrite."""
    base = ['She smiles. "Hello there," she says warmly.']
    draft = "*She steps closer, watching him carefully.* Are you sure about this?"
    assert normalize_to_baseline(draft, base, enabled=True)[0] == (
        'She steps closer, watching him carefully. "Are you sure about this?"'
    )

    # Read as quoted-dialogue prose, its bare run is narration and stays unquoted.
    source = AxisStyle(dialogue=Dialogue.QUOTED, narration=Narration.ASTERISK)
    new, rep = normalize_to_baseline(draft, base, enabled=True, source=source)
    assert rep.source is source
    assert new == "She steps closer, watching him carefully. Are you sure about this?"


# ---------- a paragraph whose quotes do not pair is never rewritten ----------


def test_an_unclosed_quote_leaves_its_paragraph_alone():
    """An unclosed quote swaps speech and narration for the rest of the paragraph."""
    draft = '"Hello, she said, stepping inside. "It\'s cold out there," he replied.'
    _assert_unchanged(draft, FULL_MARKUP_BASELINE)
    # Even a correct reading of the draft cannot make the swapped parse safe.
    source = AxisStyle(dialogue=Dialogue.QUOTED, narration=Narration.BARE)
    assert normalize_to_baseline(draft, FULL_MARKUP_BASELINE, enabled=True, source=source)[0] == draft


def test_an_even_count_quote_swap_leaves_its_paragraph_alone():
    """Two quotes can pair the wrong way round: the span opens onto a space."""
    _assert_unchanged('She waved. Hello," she said. "Goodbye.', FULL_MARKUP_BASELINE)


def test_only_the_paragraph_with_the_unpaired_quote_is_skipped():
    draft = 'She pours the tea. "Drink it while it\'s hot."\n\n"Hello, she said. "Bye," he replied.'
    new, rep = normalize_to_baseline(draft, FULL_MARKUP_BASELINE, enabled=True)
    assert rep.changed
    assert new == '*She pours the tea.* "Drink it while it\'s hot."\n\n"Hello, she said. "Bye," he replied.'


def test_measurement_marks_and_apostrophes_do_not_block_a_rewrite():
    draft = 'He is 6\'2" and the dogs’ keeper. "Sit down," he says.'
    new, rep = normalize_to_baseline(draft, FULL_MARKUP_BASELINE, enabled=True)
    assert rep.changed
    assert new == '*He is 6\'2" and the dogs’ keeper.* "Sit down," *he says.*'


def test_an_ornamental_quote_is_speech_to_the_rewriter_too():
    new, rep = normalize_to_baseline("She leans closer. ❝You came back,❞ she murmurs.", FULL_MARKUP_BASELINE, enabled=True)
    assert rep.changed
    assert new == "*She leans closer.* ❝You came back,❞ *she murmurs.*"


# ---------- the action policy (skip-v1): drafts no reading makes safe ----------


def test_skip_reasons_flag_structure_that_is_not_rp_prose():
    assert {"metadata", "rule", "speaker-label"} <= set(
        skip_reasons('REWARD: 13\n\n---\n\nRESPONSE: *AVA smiles.* "Thanks, Kane!"')
    )
    assert skip_reasons('Cecilia: "We have been *thinking*."\nCeline: "Arguing."') == ["speaker-label"]
    assert "list" in skip_reasons("*sighs* Fine.\n\nPros:\n- loyal\n- fast")
    assert skip_reasons("# Chapter One\n\n*She wakes.*") == ["heading"]
    assert skip_reasons("| hp | 10 |\n| mp | 4 |") == ["table"]


def test_skip_reasons_flag_emphasis_the_parser_mis_pairs():
    assert "stray-asterisk" in skip_reasons("*He should be *furious* by now, but he is not.*")
    assert skip_reasons('*Her throat went dry at the word "wild," her fingers tightening.*') == ["quote-in-emphasis"]


def test_bullet_stars_are_a_list_not_a_stray_asterisk():
    assert skip_reasons("Pack these:\n* rope\n* a lantern") == ["list"]
    # A bullet opens after any hard line break; a star after a mere space is a stray.
    assert skip_reasons("then\u2028* rope") == []
    assert skip_reasons("then * rope") == ["stray-asterisk"]


def test_skip_reasons_leave_ordinary_rp_prose_alone():
    for draft in (
        '*She smiles.* "Hello there," she says.',
        'She crossed the room. *He was lying again.* "Fine," she said.',
        'She said: "Fine." Then she left.',
        "*tilts head* owo",
        '**Chapter One**\n\n*She wakes.* "Morning."',
        '```\nMOOD: happy\n---\n- hp 10\n- mp 4\n```\n*She waves.* "Hi!"',  # the fence is never rewritten
    ):
        assert skip_reasons(draft) == [], draft


def test_a_skipped_draft_is_left_as_written_whatever_the_reading():
    base = ['She smiles. "Hello there," she says warmly.']
    draft = "*She steps closer, watching him carefully, one hand on the doorframe.*\n\n---\n\nAre you sure about this?"
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert (new, rep.changed, rep.note) == (draft, False, "skipped (rule)")
    assert normalize_format(draft, rep.target) != draft  # the policy held it, not the reading
    # An explicit source (the markup classifier's) is gated the same way.
    source = AxisStyle(dialogue=Dialogue.BARE, narration=Narration.ASTERISK)
    assert normalize_to_baseline(draft, base, enabled=True, source=source)[0] == draft


# ---------- voting a target over supplied readings ----------


def test_vote_axes_is_baseline_axes_over_supplied_readings():
    messages = [QUOTES_ONLY, FULL_MARKUP, QUOTES_ONLY, ""]
    assert vote_axes([classify_axes(m) for m in messages]) == baseline_axes(messages)
