"""Unit tests for the negated-narration detector (analysis/detectors/negated_narration.py).

Shape fixtures run ungated (``min_hits=0``) so each asserts one rule; the gate, run construction, and exact offsets have their
own sections. Rejected split-contrast inputs come from the measured corpus review: they assert the rejected *shape*, since a
valid null reaction may remain.
"""

from __future__ import annotations

import pytest

from backend.analysis.detectors.negated_narration import NegationResult, detect_negated_narration, evaluate_negated_narration


def _ungated(text: str) -> NegationResult:
    return detect_negated_narration(text, min_hits=0)


def _kinds(text: str) -> list[list[str]]:
    return [f.kinds for f in _ungated(text).findings]


def _first_kind(text: str) -> str | None:
    findings = _ungated(text).findings
    return findings[0].kinds[0] if findings else None


def _assert_exact(result: NegationResult) -> None:
    for f in result.findings:
        assert result.source_text[f.start : f.end] == f.span
        cursor = f.start
        for sentence in f.sentences:
            at = result.source_text.index(sentence, cursor)
            assert f.start <= at and at + len(sentence) <= f.end
            cursor = at + len(sentence)


# -- 1. Shape positives and ordinary negatives ---------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("She doesn't flinch. Doesn't stand.", "cascade"),
        ("Not a denial. Not an apology.", "cascade"),
        ("No wind-up. No warning.", "cascade"),
        ("The question isn't curious. It's accusing.", "split_contrast"),
        ("Not loud. Conversational.", "split_contrast"),
        ("She doesn't say anything else. Just waits.", "split_contrast"),
        ("She didn't scream, didn't even answer.", "stacked"),
        ("No pause, no glance at the door.", "stacked"),
        ("Not too fast, not too slow.", "stacked"),
        ("He didn't answer.", "null_reaction"),
        ("Nobody moves.", "null_reaction"),
        ("No one moves.", "null_reaction"),
        ("Neither of them speaks.", "null_reaction"),
        ("He didn't so much as blink.", "null_reaction"),
        ("But the oracle wasn't listening.", "null_reaction"),
        ("It wasn't a question.", "null_reaction"),
        ("This wasn't anger.", "null_reaction"),
        ("That wasn't a request.", "null_reaction"),
        ("She never looked back.", "null_reaction"),
        ("He did not answer.", "null_reaction"),
    ],
)
def test_required_shape_positives(text, kind):
    assert _first_kind(text) == kind


@pytest.mark.parametrize(
    "text",
    [
        "The thought wasn't hers.",
        "She wasn't dead.",
        "This isn't how it's supposed to go.",
        "Things that had never been stretched before.",
        "She didn't know the answer, so she guessed.",
        "He was unafraid and careless.",  # privatives are not negation
        "She walked without a sound.",  # `without` is out of scope
        "The arrow didn't fly toward the caster's heart—not yet.",
    ],
)
def test_ordinary_negatives_privatives_and_copula_misses(text):
    assert _first_kind(text) is None


def test_multi_clause_negative_is_not_a_null_reaction():
    assert _first_kind("He didn't answer because he was tired.") is None
    assert _first_kind("She didn't move, though her hands shook.") is None


@pytest.mark.parametrize(
    "text",
    [
        '"Fix it," she said, still not looking.',
        "She giggles, not pulling back.",
        "Her hand hovers near yours, not quite touching.",
        "She's already walking past him—never once glancing back.",
        "She wrings water from her hair, not bothering to cover herself.",
    ],
)
def test_trailing_negation_positives(text):
    assert _first_kind(text) == "trailing_negation"


@pytest.mark.parametrize(
    "text",
    [
        "Not looking, she reached for the door.",  # the denial is the main clause's lead
        "She backtracks, not wanting to seem too eager.",  # a motive, not a withheld action
        "She stops, but not before glancing back.",
        "He shrugs, not anything she'd call an answer.",
        "Her eyes never leaving yours, she kneels.",  # absolute construction
    ],
)
def test_trailing_negation_misses(text):
    assert _first_kind(text) is None


def test_trailing_negation_joins_a_null_reaction_past_the_gate():
    text = (
        "Judy didn't dignify that with a response. She moved.\n\n"
        '"Two choices, rookie," she said, still not looking. "Pull your weight."'
    )
    assert _kinds(text) == [["null_reaction"], ["trailing_negation"]]
    assert detect_negated_narration(text).raw_hits == 2


def test_null_reaction_word_limits():
    assert _first_kind("He didn't answer the question she had asked him.") == "null_reaction"  # 9 words
    assert _first_kind("He didn't answer the question she had asked him twice.") is None  # 10 words


@pytest.mark.parametrize(
    "text",
    [
        "It's cold, isn't it.",
        "She didn't mean it, did she.",
        "Didn't she say that?",
        "*Didn't she say that?*",
        "Don't stop, don't stop.",
        "Do not move.",
        "But don't look.",
        "No.",
        "No, no, no.",
        "Oh no, oh no.",
        "No—wait.",
    ],
)
def test_questions_imperatives_and_interjections_are_skipped(text):
    result = _ungated(text)
    assert result.findings == []
    assert result.narration_sentences == 0


def test_skipped_units_are_adjacency_barriers():
    # The interjection is skipped, so the denials either side stay apart.
    assert _kinds("She doesn't move. No, no, no. Doesn't breathe.") == [["null_reaction"]]


def test_interjection_match_is_the_whole_unit():
    assert _ungated("No, not shaking—jittering.").narration_sentences == 1
    assert _first_kind("No one moves.") == "null_reaction"
    assert _ungated("No wind.").negated_sentences == 1
    assert _ungated("No way.").negated_sentences == 1


@pytest.mark.parametrize(
    "text",
    [
        "She should say no.",
        "The answer was no.",
        "He asked for a yes or no.",
        "She shook her head no, then left.",
        "Not only did she stay, she laughed.",
    ],
)
def test_negative_values_and_not_only_are_not_negation(text):
    result = _ungated(text)
    assert result.negated_sentences == 0
    assert result.findings == []


def test_value_use_ignores_only_that_occurrence():
    # "no" is a value; "didn't" is still a genuine negation in the same sentence.
    assert _ungated("He didn't say yes or no.").negated_sentences == 1


def test_straight_and_curly_contractions_match_identically():
    straight = _ungated("She doesn't flinch. Doesn't stand.\n\nIt wasn't a question.")
    curly = _ungated("She doesn’t flinch. Doesn’t stand.\n\nIt wasn’t a question.")
    assert [f.kinds for f in straight.findings] == [f.kinds for f in curly.findings]
    assert straight.raw_hits == curly.raw_hits == 2


def test_word_limits_count_words_not_punctuation():
    # 12 words with heavy punctuation and markers still qualifies.
    first = "She doesn't — not *once* — look at the man, the door, or me."
    assert _first_kind(f"{first} Just waits.") == "split_contrast"
    twelve = "He doesn't look at the old man by the far door tonight."
    assert len(twelve.split()) == 12
    assert _first_kind(f"{twelve} Just waits.") == "split_contrast"
    thirteen = "He doesn't look at the old man by the far door again tonight."
    assert _first_kind(f"{thirteen} Just waits.") != "split_contrast"
    # The payoff limit is 10 words.
    assert _first_kind("He didn't answer. Just waits there by the door with folded hands tonight.") == "split_contrast"
    assert _first_kind("He didn't answer. Just waits there by the old door with folded hands tonight.") != "split_contrast"


# -- 2. Split contrast: conservative forms and measured misses -----------------


def test_split_contrast_forms():
    assert _first_kind("Not a request. An order.") == "split_contrast"
    assert _first_kind("The door isn't locked. The door is open.") == "split_contrast"
    assert _first_kind("Her voice wasn't cold. It was tired.") == "split_contrast"
    assert _first_kind("She doesn't cry. She just laughs.") == "split_contrast"
    assert _first_kind("He didn't argue. Instead, he smiled.") == "split_contrast"


def test_split_contrast_rejects_person_as_it_and_subject_change():
    assert _first_kind("She isn't angry. It's grief.") != "split_contrast"
    assert _first_kind("He doesn't move. She just watches.") != "split_contrast"
    assert _first_kind("She doesn't cry. Instead, he laughs.") != "split_contrast"


def test_bare_pronoun_start_is_not_a_split_contrast():
    assert _first_kind("It didn't fall. It drifted.") == "null_reaction"
    assert _first_kind("I don't look at it. I look at you.") == "null_reaction"


def test_not_fragment_needs_a_descriptive_fragment():
    assert _first_kind("Not yet. The door opens.") is None
    assert _first_kind("Not a word. The room waits.") is None


@pytest.mark.parametrize(
    ("text", "remaining"),
    [
        ("She should say no. It was the sensible thing.", None),
        ("Her hands weren't quite steady. She blamed the cold.", None),
        ("main( and then nothing. it just stops", None),
        ("She can't finish the thought. It's too monstrous.", "null_reaction"),
        ("She doesn't know if he'll come back. That's the worst part.", "null_reaction"),
        ("It isn't certain. That's the worst part.", "null_reaction"),
        ("Nothing comes out. She tries again.", "null_reaction"),
        ("She didn't dig. Because he'd said her name.", "null_reaction"),
    ],
)
def test_measured_split_contrast_misses_are_rejected(text, remaining):
    kinds = _kinds(text)
    assert all("split_contrast" not in k for k in kinds)
    assert (kinds[0][0] if kinds else None) == remaining


def test_split_contrast_needs_an_affirmative_second_sentence():
    # A negation anywhere in the second sentence disqualifies it, as a payoff too.
    assert _kinds("She doesn't turn. She just keeps painting, and she doesn't turn around.") == [["null_reaction"]]


# -- 3. Cascades, chaining, pivots, and the gate -------------------------------


def test_cascade_is_maximal_and_counts_once():
    result = _ungated("She doesn't jump. Doesn't gasp. Doesn't blink. Doesn't breathe.")
    assert [f.kinds for f in result.findings] == [["cascade"]]
    assert result.raw_hits == 1
    assert result.findings[0].denial_count == 4


def test_lone_cascade_with_pivot_is_suppressed_by_the_gate():
    text = "She doesn't jump. Doesn't gasp. She just slowly straightens up."
    assert _kinds(text) == [["cascade", "pivot"]]
    gated = detect_negated_narration(text)
    assert gated.findings == []
    assert gated.raw_hits == 1
    assert gated.negated_sentences == 2


def test_separate_beat_passes_the_gate_and_emits_all_findings():
    text = "She doesn't jump. Doesn't gasp. She just slowly straightens up.\n\nHe didn't answer."
    result = detect_negated_narration(text)
    assert result.raw_hits == 2
    assert [f.kinds for f in result.findings] == [["cascade", "pivot"], ["null_reaction"]]


def test_adjacent_raw_findings_merge_without_changing_raw_count():
    # The stacked sentence negates late, so it is not a head denial and the two
    # sentences are separate shapes rather than one cascade.
    result = _ungated("The man by the door stood there, not moving, not breathing. It wasn't a question.")
    assert result.raw_hits == 2
    assert result.raw_shapes == {"stacked": 1, "null_reaction": 1}
    assert [f.kinds for f in result.findings] == [["stacked", "null_reaction"]]


def test_consecutive_head_denials_are_one_cascade_not_adjacent_shapes():
    result = _ungated("He didn't answer. Not loud. Conversational.")
    assert [f.kinds for f in result.findings] == [["cascade"]]
    assert result.raw_hits == 1


def test_mixed_chain_keeps_constituent_counts():
    result = _ungated(
        "The man by the door stood there, not moving, not breathing. No one moved. Nobody spoke. She just waited."
    )
    (finding,) = result.findings
    assert finding.kinds == ["stacked", "cascade", "pivot"]
    assert [(c.kind, c.sentence_count) for c in finding.constituents] == [("stacked", 1), ("cascade", 2), ("pivot", 1)]
    assert finding.denial_count == 3
    assert result.raw_hits == 2


def test_final_split_contrast_does_not_absorb_a_pivot():
    result = _ungated("The question isn't curious. It's accusing. She just stares.")
    assert [f.kinds for f in result.findings] == [["split_contrast"]]


def test_mixed_chain_ending_in_a_denial_absorbs_a_pivot():
    # A payoff of 11-14 words is too long for a split contrast but can be a pivot.
    payoff = "He just smiled at her and turned back toward the window."
    result = _ungated(f"Not loud. Conversational. He didn't answer. {payoff}")
    assert [f.kinds for f in result.findings] == [["split_contrast", "null_reaction", "pivot"]]
    assert result.findings[0].pivot_span == payoff


def test_short_payoff_after_a_null_reaction_is_a_split_contrast():
    assert _kinds("He didn't answer. He just smiled.") == [["split_contrast"]]


def test_generic_but_is_not_absorbed():
    assert _kinds("She doesn't move. Doesn't speak. But then the door opens.") == [["cascade"]]


def test_pivot_rejects_subject_change_and_long_sentences():
    assert _kinds("She doesn't move. Doesn't speak. He just waits.") == [["cascade"]]
    long = "She just waits there by the window with her hands folded and her eyes on the road."
    assert _kinds(f"She doesn't move. Doesn't speak. {long}") == [["cascade"]]


def test_min_hits_validation_and_counters_below_gate():
    with pytest.raises(ValueError):
        detect_negated_narration("He didn't answer.", min_hits=-1)
    result = detect_negated_narration("He didn't answer. She sat down.")
    assert result.findings == []
    assert (result.raw_hits, result.narration_sentences, result.negated_sentences) == (1, 2, 1)
    assert result.density == 0.5
    assert detect_negated_narration("").density == 0.0


# -- 4. Styles, thoughts, quotes, protected regions, boundaries ----------------


def test_prose_style_excludes_standalone_thoughts_and_dialogue():
    text = "He didn't answer. \"No. I don't. I won't.\" She nodded.\n\n*It doesn't matter. Nothing matters.* She sat down."
    result = _ungated(text)
    assert result.style == "prose"
    assert [f.span for f in result.findings] == ["He didn't answer."]


def test_asterisk_style_uses_block_emphasis_as_narration():
    text = "*She doesn't move. Doesn't breathe.* I don't know what you mean. *She just stares.*"
    result = _ungated(text)
    assert result.style == "asterisk"
    # Plain text is speech here, so the pivot block is not adjacent.
    assert [f.kinds for f in result.findings] == [["cascade"]]
    assert result.findings[0].span == "*She doesn't move. Doesn't breathe.*"


def test_asterisk_whitespace_between_blocks_joins_the_run():
    text = "*She doesn't move.*  *Doesn't breathe.* *She just stares.*"
    result = _ungated(text)
    assert [f.kinds for f in result.findings] == [["cascade", "pivot"]]
    assert result.findings[0].span == text


def test_inline_emphasis_stays_inside_prose_narration():
    text = "He didn't *answer*. She doesn't *look* at him."
    result = _ungated(text)
    assert result.style == "prose"
    assert [f.span for f in result.findings] == [text]


def test_sentence_initial_emphasis_continues_the_sentence():
    text = "She stops. *Nothing* moves. Nobody speaks."
    assert [f.span for f in _ungated(text).findings] == ["*Nothing* moves. Nobody speaks."]


def test_attributed_thought_is_excluded():
    text = "She waits. *Not again,* she thinks. Nobody moves."
    result = _ungated(text)
    assert [f.span for f in result.findings] == ["Nobody moves."]


def test_balanced_multiline_quote_is_speech():
    text = "\"I don't care.\n\nI won't. Nobody will.\" He didn't answer."
    assert [f.span for f in _ungated(text).findings] == ["He didn't answer."]


def test_multi_paragraph_quote_convention_is_speech():
    text = "\"I don't care. Nobody does.\n\n\"I won't go. Never.\" He didn't answer."
    assert [f.span for f in _ungated(text).findings] == ["He didn't answer."]


def test_malformed_quote_never_turns_speech_into_narration():
    text = 'She says, "I don\'t. I won\'t.\n\nHe didn\'t answer. "Nobody moves," she adds. "Nothing happens.'
    result = _ungated(text)
    for f in result.findings:
        assert "I don't" not in f.span and "Nothing happens" not in f.span and "Nobody moves" not in f.span


def test_fenced_content_is_excluded_even_when_unclosed():
    text = "```\nHe didn't answer. Nobody moved.\n```\nShe sat.\n\n```\nIt doesn't work. It can't."
    assert _ungated(text).findings == []


def test_html_comment_and_ooc_blocks_are_excluded():
    text = (
        "<div class='status'>He didn't answer. Nobody moved.</div>\n\n<!-- It doesn't work. It can't. -->\n\n"
        "[OOC: I don't know. I can't say.] She sat."
    )
    result = _ungated(text)
    assert result.findings == []
    assert result.narration_sentences == 1


def test_unclosed_ooc_excludes_its_tail():
    assert _ungated("She sat. [OOC: I don't know. I can't say.").findings == []


def test_markup_and_macros_skip_the_sentence():
    assert _ungated("He didn't answer {{user}}. Nobody moved.").findings[0].span == "Nobody moved."
    assert _ungated("It doesn't load at https://example.com today.").findings == []


def test_dialogue_and_paragraph_boundaries_end_runs():
    across_dialogue = "She doesn't move. \"Stop.\" Doesn't breathe."
    assert all(len(f.sentences) == 1 for f in _ungated(across_dialogue).findings)
    across_paragraph = "She doesn't move.\n\nDoesn't breathe."
    assert all(len(f.sentences) == 1 for f in _ungated(across_paragraph).findings)
    across_line = "She doesn't move.\nDoesn't breathe."
    assert _kinds(across_line) == [["cascade"]]


def test_divider_is_a_barrier():
    assert _kinds("She doesn't move.\n***\nDoesn't breathe.") == [["null_reaction"]]


# -- 5. Complete sentence targets ----------------------------------------------


def test_regression_12196_target_includes_inline_emphasis_payoff():
    text = "His grip didn't budge. It didn't even tense up. It just *held*."
    (finding,) = _ungated(text).findings
    assert finding.kinds == ["cascade", "pivot"]
    assert finding.span == text
    assert finding.pivot_span == "It just *held*."


def test_regression_40656_sentence_is_never_cut_at_emphasis():
    text = "It didn't fall. It didn't sink. It just drifted *up*, turning lazily, catching the light."
    (finding,) = _ungated(text).findings
    assert finding.span == text
    unmarked = "It didn't fall. It drifted *up*, turning lazily, catching the light."
    for f in _ungated(unmarked).findings:
        assert not f.span.endswith("It drifted")
        assert f.span == "It didn't fall."


def test_emphasis_never_truncates_a_long_sentence_to_fit():
    text = "She didn't blink. She just *kept* looking at him across the long table with her hands folded and still."
    (finding,) = _ungated(text).findings
    assert finding.kinds == ["null_reaction"]  # the payoff is too long, not truncated
    assert finding.span == "She didn't blink."


def test_double_spaces_unicode_and_outer_markers_are_preserved():
    text = "Café lights. Zoë doesn’t  move.  Doesn’t  breathe. Ñandú just waits."
    result = _ungated(text)
    _assert_exact(result)
    (finding,) = result.findings
    assert finding.span == "Zoë doesn’t  move.  Doesn’t  breathe. Ñandú just waits."


# -- 6. Exact offsets for repeated text ----------------------------------------


def test_offsets_for_text_repeated_in_an_earlier_thought():
    text = "*He didn't answer.*\n\nShe waits. He didn't answer. Nobody moved."
    result = _ungated(text)
    _assert_exact(result)
    (finding,) = result.findings
    assert finding.start == text.rindex("He didn't answer.")


def test_offsets_for_repeated_beats():
    beat = "He didn't answer."
    text = f"She doesn't move. Doesn't breathe. {beat}\n\nThe fire crackles.\n\n{beat}"
    result = _ungated(text)
    _assert_exact(result)
    starts = [f.start for f in result.findings]
    assert starts == [0, text.rindex(beat)]


# -- 8. Draft-only input and evaluation style ----------------------------------


def test_evaluation_reuses_an_explicit_style():
    draft = "*She doesn't move. Doesn't breathe.* Hello there, you. *She waits.*"
    assert detect_negated_narration(draft).style == "asterisk"
    shortened = "*She stays.* Hello there, you. *She waits.*"
    # A shortened draft could re-infer a different style; evaluation must not.
    assert evaluate_negated_narration(shortened, "asterisk").style == "asterisk"
    assert evaluate_negated_narration(shortened, "prose").style == "prose"
    with pytest.raises(ValueError):
        evaluate_negated_narration(shortened, "emphasis")


def test_evaluation_is_ungated_by_default():
    assert evaluate_negated_narration("He didn't answer.", "prose").findings
