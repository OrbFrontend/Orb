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
    normalize_format,
    normalize_to_baseline,
)

QUOTES_ONLY = 'She smiles and steps back. "I won\'t go," she says, turning to the window.'
ASTERISKS_ONLY = "*She smiles and steps back, turning to the window.* I won't go."
FULL_MARKUP = '*He leans on the doorframe, arms crossed.* "You came back," *he murmurs.*'


def test_axis_enums_render_as_wire_values():
    assert str(Dialogue.QUOTED) == "quoted"
    assert f"{Narration.ASTERISK}" == "asterisk"


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


# ---------- no-op safety ----------


def test_already_consistent_is_byte_identical():
    base = ['She smiles. "Hello there," she says warmly.']
    draft = 'He nods slowly. "I understand," he replies.'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft


def test_embedded_thought_message_is_noop_against_quotes_baseline():
    base = ['She smiles. "Hello there," she says warmly.']
    draft = (
        "She paused at the door, one hand on the frame. "
        "*Was he really serious about this?* "
        '"Tell me the truth," she said quietly.'
    )
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft


def test_disabled_is_noop():
    base = ["*She smiles.* Hello there."]
    draft = 'She smiles. "Hello there."'
    new, rep = normalize_to_baseline(draft, base, enabled=False)
    assert not rep.changed
    assert new == draft
    assert rep.note == "disabled"


def test_no_baseline_is_noop():
    draft = 'She smiles. "Hello there."'
    new, rep = normalize_to_baseline(draft, [], enabled=True)
    assert not rep.changed
    assert new == draft


def test_unstable_baseline_is_noop():
    # One quotes-only, one asterisks-only -> neither axis agrees -> no enforcement.
    base = [QUOTES_ONLY, ASTERISKS_ONLY]
    target = baseline_axes(base)
    assert target.dialogue == Dialogue.UNKNOWN
    assert target.narration == Narration.UNKNOWN
    draft = 'She frowns. "What now?"'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft


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
    base = [
        'She smiles. "Hello there," she says warmly.',
        'He nods. "Welcome back," he replies.',
    ]
    draft = '*He leans in close.* "You came back." ***He could not believe it.***'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    # The `***…***` run survives verbatim; only the prose narration asterisks go.
    assert new == 'He leans in close. "You came back." ***He could not believe it.***'


def test_bold_italic_run_left_byte_identical_when_prose_is_consistent():
    base = [
        'She smiles. "Hello there," she says warmly.',
        'He nods. "Welcome back," he replies.',
    ]
    draft = '***She steps closer, watching him.*** "Are you sure?"'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    # The `***…***` is protected and the quoted dialogue already matches: no-op.
    assert not rep.changed
    assert new == draft


def test_four_asterisk_run_preserved():
    base = [
        'She smiles. "Hello there," she says warmly.',
        'He nods. "Welcome back," he replies.',
    ]
    draft = "He was ****really**** angry."
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft  # ****really**** carried through intact


def test_scene_divider_run_preserved_with_surrounding_text():
    base = [
        'She smiles. "Hello there," she says warmly.',
        'He nods. "Welcome back," he replies.',
    ]
    draft = "She turns away.\n\n***\n\nThe room falls silent."
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft  # divider and its blank-line spacing untouched


def test_no_asterisk_run_stays_byte_identical():
    # A draft with no 3+ run is unaffected: single-* emphasis and already-consistent
    # text still come back untouched.
    base = ['She smiles. "Hello there," she says warmly.']
    draft = 'He frowns. "Do you think I am *stupid*?"'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft


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
    base = [
        'She smiles. "Hello there," she says warmly.',
        'He nods. "Welcome back," he replies.',
    ]
    draft = '*He leans in.* "You came back."\n\n```python\nx = a ***b*** c  # not RP markup\n```'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed  # the prose narration asterisks were stripped
    assert new == 'He leans in. "You came back."\n\n```python\nx = a ***b*** c  # not RP markup\n```'


def test_asterisk_runs_preserved_in_both_prose_and_code():
    # A `***…***` run is protected whether it sits in prose or inside a fence; both
    # survive verbatim and the bare narration around them is already consistent.
    base = [
        'She smiles. "Hello there," she says warmly.',
        'He nods. "Welcome back," he replies.',
    ]
    draft = "She turns. ***Important.***\n\n```\nkeep ***this***\n```"
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft


def test_code_only_draft_is_byte_identical():
    base = ['She smiles. "Hello there," she says warmly.']
    draft = "```\n*not* RP markup, ***at all***\n```"
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft


# ---------- *emphasis* inside dialogue (LLMs do this constantly) ----------


def test_emphasis_in_dialogue_is_noop_against_quotes_baseline():
    # `*stupid*` is emphasis, not narration; against a quotes baseline the turn is
    # already consistent and must come back byte-identical.
    base = ['She smiles. "Hello there," she says warmly.']
    draft = 'He frowns. "Do you think I am *stupid*?"'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft


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
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft


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
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft


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
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft  # "Are you sure about this?" stays bare dialogue


# ---------- punctuation / glyph preservation across a rewrite ----------


def test_ellipsis_survives_dialogue_rewrite():
    base = ["*She waves.* Hi there."]  # asterisk baseline, bare dialogue
    draft = 'She hesitates. "I... I am not sure about this."'
    new, _ = normalize_to_baseline(draft, base, enabled=True)
    assert "..." in new


def test_em_dash_survives_narration_rewrite():
    base = ['She smiles. "Hello there."']  # quotes baseline, bare narration
    draft = "*He pauses — caught off guard — then steps forward.* What now?"
    new, _ = normalize_to_baseline(draft, base, enabled=True)
    assert "—" in new


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


def test_multiple_quoted_beats_all_stripped_for_asterisk_baseline():
    base = ["*She paces the room.* Right then."]  # asterisk baseline, bare dialogue
    draft = '"Wait," he said. *He grabbed her wrist.* "Do not go."'
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert rep.changed
    assert '"' not in new
    assert "Wait" in new and "Do not go" in new


def test_asterisk_narration_without_quotes_is_ambiguous_noop():
    # Asterisk narration with bare beats but no quotes at all: the bare runs could be
    # action or unquoted dialogue, so the dialogue axis reads UNKNOWN and the
    # normalizer leaves the whole turn alone rather than guessing.
    base = ['She smiles. "Hello there."']  # quotes baseline
    draft = "*She steps closer.* Are you sure? *She hesitates.* Really sure?"
    style = classify_axes(draft)
    assert style.dialogue == Dialogue.UNKNOWN
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft


# ---------- classification edge cases ----------


def test_smart_quotes_classified_as_quoted_dialogue():
    text = "She smiles and steps back. “I won’t go,” she says."
    style = classify_axes(text)
    assert style.dialogue == Dialogue.QUOTED


def test_single_word_message_is_ambiguous_noop():
    base = ['She smiles. "Hello there."']
    draft = "Okay."
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft


def test_empty_draft_is_noop():
    base = ['She smiles. "Hello there."']
    new, rep = normalize_to_baseline("", base, enabled=True)
    assert not rep.changed
    assert new == ""


def test_whitespace_only_draft_is_noop():
    base = ['She smiles. "Hello there."']
    draft = "   \n  "
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert not rep.changed
    assert new == draft
