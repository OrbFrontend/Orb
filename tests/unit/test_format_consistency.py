"""Tests for the RP format-consistency normalizer.

Covers axis classification, the per-axis deterministic rewrite (including the
full inversion the user reported), and the conservative no-op behaviour that
keeps the feature safe (ambiguous input, unstable baseline, disabled).
"""

from backend.analysis.format_consistency import (
    AxisStyle,
    Dialogue,
    Narration,
    baseline_axes,
    classify_axes,
    narration_only,
    normalize_format,
    normalize_to_baseline,
    stable_label,
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
    # A consistent quotes-only message with one italic thought must NOT be read as
    # asterisk-style narration (the bug the two-axis coverage model fixes).
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
    # Dialogue axis already matched (both quote dialogue) -> quotes untouched.
    assert new == '*He leans against the wall.* "You came back."'


def test_full_markup_to_quotes_only_strips_narration_asterisks():
    target = AxisStyle(dialogue=Dialogue.QUOTED, narration=Narration.BARE)
    out = normalize_format(FULL_MARKUP, target)
    assert "*" not in out.replace("*he murmurs.*", "")  # block narration unwrapped
    assert '"You came back,"' in out  # dialogue untouched


# The recurring baseline for these cases: quoted dialogue, bare narration,
# already internally consistent — so any change a draft causes is the draft's.
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
    # One quotes-only, one asterisks-only -> neither axis agrees -> no enforcement.
    base = [QUOTES_ONLY, ASTERISKS_ONLY]
    target = baseline_axes(base)
    assert target.dialogue == Dialogue.UNKNOWN
    assert target.narration == Narration.UNKNOWN
    draft = 'She frowns. "What now?"'
    _assert_unchanged(draft, base)


# ---------- preservation of incidental markup ----------


def test_contractions_survive():
    base = ["*She waves.* Hi there."]  # asterisk baseline
    draft = "She can't believe it. \"I won't leave,\" she insists."
    new, _ = normalize_to_baseline(draft, base, enabled=True)
    assert "can't" in new
    assert "won't" in new


def test_markdown_bullets_not_treated_as_emphasis():
    text = "Here is a list:\n* first item\n* second item\nThat is all."
    # The leading-bullet guard means these are bare narration, not emphasis.
    assert classify_axes(text).narration != Narration.ASTERISK


def test_asterisk_inside_quotes_is_not_narration():
    text = 'He said, "you are *so* dramatic," and rolled his eyes as she huffed.'
    # The `*so*` lives inside dialogue, so it must not register as narration markup.
    style = classify_axes(text)
    assert style.dialogue == Dialogue.QUOTED


# ---------- 3+ asterisk runs (markdown bold-italic / scene dividers) ----------
# `***x***` and `****` are not single-* RP markup, and the parser can't represent
# them. They are protected runs: excluded from classification and carried through the
# rewrite verbatim, so they neither corrupt the read nor get dropped -- a `***` the
# author typed is still there afterwards, while the surrounding prose still normalizes.


def test_bold_italic_run_preserved_while_prose_normalizes():
    # The reported breakage: a `***…***` block beside real markup used to come back
    # mangled. Now the run is carried through untouched and the rest normalizes.
    base = QUOTES_BASELINE
    draft = '*He leans in close.* "You came back." ***He could not believe it.***'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    # The `***…***` run survives verbatim; only the prose narration asterisks go.
    assert new == 'He leans in close. "You came back." ***He could not believe it.***'


def test_scene_divider_run_preserved_with_surrounding_text():
    base = QUOTES_BASELINE
    draft = "She turns away.\n\n***\n\nThe room falls silent."
    _assert_unchanged(draft, base)  # divider and its blank-line spacing untouched


# ---------- fenced code blocks (literal content, never reformatted) ----------
# Markup inside ```...``` is literal text, not RP prose: it must not sway the axes
# and must survive the rewrite byte-for-byte (including 3+ asterisk runs).


def test_code_block_markup_does_not_sway_classification():
    # The `*...*` / `***...***` live inside a fence, so the narration axis is read
    # only from the surrounding bare prose, not flipped to ASTERISK.
    text = "She nods.\n\n```\n*this is code* and ***bold*** stuff\n```\n\nShe leaves."
    style = classify_axes(text)
    assert style.narration != Narration.ASTERISK


def test_code_block_passes_through_rewrite_verbatim():
    base = QUOTES_BASELINE
    draft = '*He leans in.* "You came back."\n\n```python\nx = a ***b*** c  # not RP markup\n```'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed  # the prose narration asterisks were stripped
    assert new == 'He leans in. "You came back."\n\n```python\nx = a ***b*** c  # not RP markup\n```'


# ---------- *emphasis* inside dialogue (LLMs do this constantly) ----------


def test_emphasis_in_dialogue_survives_narration_strip():
    # The key invariant: stripping narration asterisks must remove only the asterisks
    # *outside* the quotes, never the in-dialogue emphasis.
    base = ['She smiles. "Hello there."']  # quotes baseline, bare narration
    draft = '*He leans in.* "Do you think I am *stupid*?"'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert new == 'He leans in. "Do you think I am *stupid*?"'
    assert "*stupid*" in new  # emphasis preserved
    assert new.count("*") == 2  # only the emphasis pair remains


def test_emphasis_in_dialogue_not_misread_as_narration_axis():
    # Even with multiple emphasis spans, the narration axis must not flip to ASTERISK.
    text = '"You are *so* dramatic," he said, "and *always* late."'
    style = classify_axes(text)
    assert style.dialogue == Dialogue.QUOTED
    assert style.narration != Narration.ASTERISK


def test_emphasis_survives_dialogue_flattening_against_asterisk_baseline():
    # Soft corner: flattening quotes to bare in an asterisk chat keeps the emphasis
    # (no data loss), even though it now sits beside asterisk narration. We assert
    # content survival, not a clean separation, because none is possible here.
    base = [
        "*She smiles, stepping back.* Hello there.",
        "*He follows her in.* Good to see you.",
    ]
    draft = 'He frowns. "Do you think I am *stupid*?"'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert "*stupid*" in new  # emphasis content not lost
    assert "stupid" in new


def test_multiparagraph_preserves_separators():
    base = ['*She nods.* "Okay."']  # full-ish / asterisk narration baseline
    draft = "She nods slowly.\n\nShe steps away from the table."
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    # Both paragraphs rewritten, blank-line separator intact.
    assert "\n\n" in new
    if rep.changed:
        assert new.count("\n\n") == draft.count("\n\n")


def test_inline_emphasis_inside_narration_not_fragmented():
    # quotes-only narration -> asterisk narration: the inline *really* is absorbed
    # into the single wrapped run, not left as its own *really* fragment.
    base = ['*He waited by the window, tense.* "Where were you?"']  # full-markup baseline
    draft = '"Where is he?" She was *really* nervous about the whole thing.'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert new == '"Where is he?" *She was really nervous about the whole thing.*'
    assert "*really*" not in new  # not fragmented


def test_pure_bare_narration_without_dialogue_is_noop():
    # No quotes and no asterisks means bare text is ambiguous (narration vs. bare
    # dialogue in an asterisk-only chat), so the normalizer leaves it alone.
    base = ["*She paces the room nervously, glancing at the clock.* Right."]
    draft = "She was really nervous about the whole thing."
    _assert_unchanged(draft, base)


# ---------- multi-turn baselines (the cross-message regression) ----------


def test_stable_multiturn_quotes_baseline_converts_asterisk_turn():
    # Several consistent quotes-only turns establish the style; the next turn drifts
    # to asterisk narration and must be pulled back.
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
    # Baseline is solidly quotes-only; a new quotes-only turn stays byte-identical.
    base = [
        'She smiles. "Hello there."',
        'He nods. "Welcome back."',
    ]
    draft = 'She tilts her head. "What brings you here?"'
    _assert_unchanged(draft, base)


# ---------- within-message drift (dominant style matches, a few spans do not) ----------
# A draft whose *dominant* style already matches the baseline used to short-circuit to
# a no-op, leaving a stray bare narration beat or paragraph un-normalized. The rewrite
# now enforces the convention span-by-span while keeping already-correct spans intact.

FULL_MARKUP_BASELINE = [
    '*He leaned against the bookshelf, watching her.* "You came back," *he murmured.*',
    '*She set down the book, fingers trembling.* "I told you I would," *she said softly.*',
    '*He crossed the room in three steps.* "Then prove it," *he whispered.*',
]


def test_stray_bare_dialogue_tag_is_wrapped_when_dominant_style_matches():
    # Dominantly full-markup (quoted dialogue + asterisk narration) -- matching the
    # baseline -- but the dialogue tag `she replied.` was left bare. It must be
    # wrapped, while the surrounding asterisk narration stays byte-for-byte.
    draft = (
        "*She pulled the curtain aside and glanced out at the street.*\n\n"
        '"It looks like it might rain," she replied.\n\n'
        "*She let the fabric fall back and turned to the window latch.*"
    )
    new, rep = normalize_to_baseline(draft, FULL_MARKUP_BASELINE, enabled=True)
    assert rep.changed
    assert '"It looks like it might rain," *she replied.*' in new
    # Untouched paragraphs survive verbatim.
    assert "*She pulled the curtain aside and glanced out at the street.*" in new
    assert "*She let the fabric fall back and turned to the window latch.*" in new


def test_quoteless_full_markup_draft_wraps_bare_paragraph_in_asterisks_not_quotes():
    # No dialogue this turn, so the draft has no quotes and self-reads as bare-dialogue
    # asterisk convention. Against a full-markup baseline its lone un-asterisked
    # paragraph is narration: it must gain asterisks, never quotes.
    draft = (
        "*Jane traced a finger along the spines of the books on the shelf.*\n\n"
        "The librarian tilted her head, her blue eyes scanning the titles.\n\n"
        "*She slid one volume free and weighed it in her hands.*"
    )
    new, rep = normalize_to_baseline(draft, FULL_MARKUP_BASELINE, enabled=True)
    assert rep.changed
    assert '"' not in new  # the narration paragraph was NOT mistaken for dialogue
    assert "*The librarian tilted her head, her blue eyes scanning the titles.*" in new


def test_genuine_asterisk_convention_draft_is_not_misread_as_full_markup():
    # The mirror safety case: against a *bare-dialogue* asterisk baseline, a quoteless
    # draft's bare runs are spoken lines, not narration -- they must stay bare, never
    # get wrapped in asterisks.
    base = [
        "*She smiles, stepping back.* Hello there.",
        "*He follows her in.* Good to see you.",
        "*She turns to face him.* It has been too long.",
    ]
    draft = "*He shifts his weight.*\n\nAre you sure about this?\n\n*She waits.*"
    _assert_unchanged(draft, base)  # "Are you sure about this?" stays bare dialogue


# ---------- a draft split between the two narration conventions ----------
# The reported case: the model was told to wrap narration in asterisks, the baseline
# had none, and the draft came back roughly half-and-half. That ratio lands in the
# ``classify_axes`` dead band, so the draft's own narration axis reads UNKNOWN.
#
# The two directions out of that band are not equally safe, so the normalizer treats
# them differently. Wrapping the unmarked runs completes a draft that was going that
# way anyway and leaves the marked spans alone. Unwrapping the marked ones is a guess
# about what they mean -- an action beat and an italic thought have the same shape --
# so the dead band is taken at its word and the draft is left as it is.

HALF_ASTERISKED_DRAFT = (
    "Amaryllis blinked, caught off guard by how serious the question was.\n\n"
    "Her fingers tightened around the spine of the book.\n\n"
    "*She let out a short, breathy laugh, the first in weeks.*\n\n"
    '"Battle scenes?" *she asked, her accent lilting.* "Aye, there\'s a few."\n\n'
    '*She cleared her throat and looked away.* "There\'s a siege in the third act."'
)


def test_half_asterisked_draft_classifies_into_the_dead_band():
    # The precondition the rest of this section rests on: neither threshold is met, so
    # the draft has no narration convention of its own to compare against a baseline.
    assert classify_axes(HALF_ASTERISKED_DRAFT).narration == Narration.UNKNOWN


BARE_NARRATION_BASELINE = [
    'She set the book down. "I told you I would," she said softly.',
    'He crossed the room in three steps. "Then prove it," he whispered.',
    'She looked away. "It doesnae matter," she muttered.',
]


def test_half_asterisked_draft_is_left_alone_against_a_bare_narration_baseline():
    # The destructive direction, and the one the dead band has to veto. Every
    # ``*...*`` here happens to be an action beat, but nothing in the text says so:
    # the same shape carries an italic thought two tests down, and unwrapping
    # cannot tell the two apart. So the draft keeps its markers and the drift with
    # them -- a missed repair rather than a mangled reply.
    _assert_unchanged(HALF_ASTERISKED_DRAFT, BARE_NARRATION_BASELINE)


def test_half_asterisked_draft_is_completed_against_an_asterisk_baseline():
    # The mirror direction: the same unsettled draft against a baseline that does use
    # asterisk narration gains the markers it is missing rather than losing the ones
    # it has.
    new, rep = normalize_to_baseline(HALF_ASTERISKED_DRAFT, FULL_MARKUP_BASELINE, enabled=True)
    assert rep.changed
    assert "*Amaryllis blinked, caught off guard by how serious the question was.*" in new
    assert "*She let out a short, breathy laugh, the first in weeks.*" in new
    assert '"Battle scenes?"' in new


# ---------- punctuation / glyph preservation across a rewrite ----------


def test_question_and_exclamation_preserved_through_inversion():
    base = ["*She smiles, stepping back.* Hello there."]  # asterisk baseline
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
    assert '"Quite a storm out there,"' in new  # already-correct dialogue untouched


def test_mixed_draft_against_asterisk_baseline_strips_only_dialogue_quotes():
    base = [
        "*She smiles, stepping back.* Hello there.",
        "*He follows her in.* Welcome back.",
    ]
    draft = '*He steps inside, shaking off the rain.* "Quite a storm out there," he says.'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert '"' not in new
    assert "*He steps inside, shaking off the rain.*" in new  # narration untouched


# ---------- multiple dialogue beats in one turn ----------


def test_asterisk_narration_without_quotes_reads_as_bare_dialogue():
    # Asterisk beats plus unmarked runs and no quotes anywhere: the beats are stage
    # directions and the unmarked runs address someone, so this is the bare-dialogue
    # convention and converting it to a quotes baseline is safe -- the quotes take
    # over the job the asterisks were doing.
    base = ['She smiles. "Hello there."']  # quotes baseline
    draft = "*She steps closer.* Are you sure? *She hesitates.* Really sure?"
    assert classify_axes(draft).dialogue == Dialogue.BARE
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert new == 'She steps closer. "Are you sure?" She hesitates. "Really sure?"'


def test_italic_thoughts_in_prose_are_not_read_as_bare_dialogue():
    # The competing convention with the same markup shape: third-person prose whose
    # asterisks are quoted thought, not action. The unmarked runs describe the
    # character rather than address anyone, so the dialogue axis stays UNKNOWN and
    # the asterisks -- load-bearing here -- are left alone.
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
    """Person and convention are orthogonal, so changing only the narrator's person
    must not change the markup answer.

    The regression: the sub-threshold reading used to weigh first/second-person
    words against third-person ones, so prose narration that said "my fingers"
    outvoted its own italic-thought evidence and read as bare dialogue -- while the
    identical passage saying "her fingers" read as UNKNOWN. Against a quotes
    baseline that misread wrapped the narration in quotes and unwrapped the thought,
    turning narration into speech.
    """
    thought = "*He still likes me. He really does.*"
    third = f"Sayori's phone slips from her numb fingers.\n\n{thought}\n\nThe thought is not a comfort."
    first = f"My phone slips from my numb fingers.\n\n{thought}\n\nThe thought is not a comfort."

    assert classify_axes(third) == classify_axes(first)
    assert classify_axes(first).dialogue == Dialogue.UNKNOWN
    # And the no-op that follows from it, which is what the reader actually loses.
    for draft in (third, first):
        _assert_unchanged(draft, ['She smiles. "Hello there."'])


def test_an_italic_aside_written_from_outside_is_still_not_bare_dialogue():
    """The span test alone would pass this: "Everything had changed." is a clause
    with no first-person marker. The bare runs veto it instead -- they describe the
    character in the third person, so they are narration and the asterisks are not
    the only thing marking it."""
    draft = "She walked to the window. *Everything had changed.* The street below was empty."
    assert classify_axes(draft).dialogue == Dialogue.UNKNOWN
    _assert_unchanged(draft, ['She smiles. "Hello there."'])


def test_an_attributed_line_in_the_bare_runs_vetoes_the_bare_dialogue_read():
    """The person-free half of the veto, and the one that still works when the
    narrator is first person: a speaker cannot attribute her own line from inside
    it, so "I said" marks the run around it as narration."""
    draft = "I set the cup down. *This was going badly.* Fine, I said, and looked away."
    assert classify_axes(draft).dialogue == Dialogue.UNKNOWN


def test_a_talkative_bare_dialogue_baseline_still_sets_the_axes():
    # The reported case. The greeting is mostly unmarked speech with one short action
    # beat, so the coverage ratio reads it as 13% asterisked -- nowhere near the
    # threshold -- because the speech it measures against is not narration at all.
    # The baseline must still come out as the bare-dialogue convention, or a reply
    # that arrives in quotes has nothing to be held to.
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
    # The axes are voted independently, so a mixed window can land on a pairing no
    # single message could produce: one bare-dialogue message carries the dialogue
    # axis while two prose messages carry the narration axis. Unmarking the speech
    # of a draft whose narration is also unmarked would leave nothing to tell them
    # apart, so a window that disagreed with itself enforces nothing.
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
# `stable_label` is the one piece the workflow's voice half shares with this
# markup half, so it is tested directly on plain strings rather than only through
# `baseline_axes`: the voice axes ("third", "past") are not this module's enums.


def test_stable_label_trusts_a_single_confident_sample():
    assert stable_label(["third"], "ambiguous") == "third"


def test_stable_label_ignores_the_unknown_sentinel():
    assert stable_label(["ambiguous", "past", "ambiguous"], "ambiguous") == "past"


def test_stable_label_is_unknown_when_nothing_is_confident():
    assert stable_label(["ambiguous", "ambiguous"], "ambiguous") == "ambiguous"
    assert stable_label([], "ambiguous") == "ambiguous"


def test_stable_label_needs_a_60_percent_majority():
    # 2 of 3 is 66% -> enforced.
    assert stable_label(["third", "third", "first"], "ambiguous") == "third"
    # 2 of 4 is 50% -> the window has not settled, so nothing is enforced.
    assert stable_label(["third", "third", "first", "second"], "ambiguous") == "ambiguous"


def test_stable_label_needs_two_occurrences_not_just_a_plurality():
    # A one-vote plurality over singletons is not agreement: every value is seen
    # once, so the majority value never reaches the two-occurrence floor.
    assert stable_label(["third", "first", "second"], "ambiguous") == "ambiguous"


def test_stable_label_rejects_an_even_split():
    assert stable_label(["past", "present"], "ambiguous") == "ambiguous"


# ---------- unknown narration is never unwrapped ----------
# The asymmetry, stated on its own. A `*...*` span in a draft whose narration axis
# is UNKNOWN may be a stage direction or an italic thought, and this module has no
# semantics to tell them apart -- so the direction that deletes markers stops at
# the dead band while the direction that adds them does not.

THOUGHT_IN_QUOTED_PROSE = 'She crossed the room slowly. *He still loves me. He has to.* "Good night," she said.'


def test_a_thought_in_an_unsettled_draft_survives_a_bare_narration_baseline():
    # Quoted dialogue, so the dialogue axis is confident -- which used to be enough
    # to license the strip and turn the thought into ordinary narration.
    style = classify_axes(THOUGHT_IN_QUOTED_PROSE)
    assert style.dialogue == Dialogue.QUOTED
    assert style.narration == Narration.UNKNOWN

    _assert_unchanged(THOUGHT_IN_QUOTED_PROSE, BARE_NARRATION_BASELINE)


def test_a_settled_bare_narration_draft_is_still_stripped():
    # The veto is the dead band's, not a blanket one: a draft that did settle on
    # asterisk narration still loses it against a bare-narration baseline.
    draft = '*He steps inside, shaking off the rain.* "Quite a storm out there," he says.'
    assert classify_axes(draft).narration == Narration.ASTERISK
    new, rep = normalize_to_baseline(draft, BARE_NARRATION_BASELINE, enabled=True)
    assert rep.changed
    assert "*" not in new


def test_wrapping_bare_narration_does_not_swallow_a_marked_span():
    # The additive direction stays additive. All three spans share the narration
    # role, so grouping them into one run would hand `_wrap_asterisks` a range
    # whose inner markers it strips -- merging a thought into the narration around
    # it while claiming to add markup.
    draft = 'She walked. *He still loves me.* She stopped. "Hello," she said.'
    new, rep = normalize_to_baseline(draft, FULL_MARKUP_BASELINE, enabled=True)
    assert rep.changed
    assert new == '*She walked.* *He still loves me.* *She stopped.* "Hello," *she said.*'
