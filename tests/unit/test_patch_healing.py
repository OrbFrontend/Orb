"""Healing for patches that restate the draft around their target span.

Models mis-aim ``editor_apply_patch`` in three recurring ways — rewriting the
sentence *before* the flagged one, handing back the flagged sentence plus the
one *after* it, or restating the lead-in of the flagged sentence's own line
(spans stop at block boundaries, so a flagged dialogue tag leaves the dialogue
in the draft). All three splice into visible duplication, so the replacement is
trimmed of any run of words that already exists against that end of the span.

The trims are exact-after-normalisation on purpose; the "leaves alone" tests
below are the half of the contract that stops healing from eating real prose.
"""

from __future__ import annotations

import pytest

from backend.analysis import Target, apply_id_patches
from backend.analysis.healing import heal_replacement


def _patch(draft: str, span: str, replace: str, *, start: int | None = None) -> tuple[str, list[str]]:
    """Apply one patch over *span*'s first occurrence in *draft*."""
    begin = draft.index(span) if start is None else start
    target = Target(tid=1, span=span, start=begin, end=begin + len(span))
    return apply_id_patches(draft, [target], [{"id": 1, "replace": replace}])


# ── The three reported mis-aims ───────────────────────────────────────────────


def test_replacement_restating_the_previous_sentence_is_rejected():
    # The model dropped the dialogue tag by rewriting the line it was not asked
    # to touch. Splicing verbatim printed `"I'm bored." "I'm bored."`.
    draft = '"I\'m bored." She murmured.'
    out, errors = _patch(draft, "She murmured.", '"I\'m bored."')
    assert out == draft
    assert errors == [
        "Error: the patch for id 1 only repeats text that already surrounds the flagged span "
        "— send new prose for the flagged text itself."
    ]


def test_replacement_swallowing_the_next_sentence_is_trimmed():
    draft = "The air smelled like ozone. The only sound was the wind."
    out, errors = _patch(draft, "The air smelled like ozone.", "The air was crisp. The only sound was the wind.")
    assert out == "The air was crisp. The only sound was the wind."
    assert errors == []


def test_replacement_restating_the_dialogue_it_was_not_given_is_trimmed():
    # The reported case. Only the tag was flagged, so only the tag was cut out —
    # the dialogue the model echoed back in front of it is still in the draft.
    draft = '"I am not... flaky," I choke out, my voice small and thin.'
    out, errors = _patch(
        draft,
        "I choke out, my voice small and thin.",
        '"I am not... flaky," I gasp, the words strained and thin.',
    )
    assert out == '"I am not... flaky," I gasp, the words strained and thin.'
    assert errors == []


def test_partial_overlap_is_trimmed_on_the_tail_side_too():
    # The mirror image: the replacement runs on into the clause that follows the
    # span, which stops mid-sentence for the same block-boundary reason.
    draft = "She stared at the door, waiting for it to open."
    out, errors = _patch(draft, "She stared at the door,", "She watched the door, waiting for it")
    assert out == "She watched the door, waiting for it to open."
    assert errors == []


def test_a_whole_line_restatement_leaves_the_flagged_text_unchanged():
    # Echoing the line back verbatim heals down to the flagged tag itself, which
    # edits nothing — one error, not a silently applied patch.
    draft = '"I am not... flaky," I choke out, my voice small and thin.'
    span = "I choke out, my voice small and thin."
    out, errors = _patch(draft, span, draft)
    assert out == draft
    assert errors == ["Error: the patch for id 1 is a no-op — `replace` repeats the flagged text unchanged."]


# ── Shape of the trim ─────────────────────────────────────────────────────────


def test_both_ends_are_trimmed_in_one_patch():
    draft = "Alpha one. Beta two. Gamma three."
    out, errors = _patch(draft, "Beta two.", "Alpha one. Delta four. Gamma three.")
    assert out == "Alpha one. Delta four. Gamma three."
    assert errors == []


def test_longest_overlap_wins_over_the_nearest_one():
    # Testing the shortest overlap first would compare "B." against "Keep",
    # miss, and leave both copies in the draft.
    draft = "Bad line. Keep A. Keep B."
    out, _ = _patch(draft, "Bad line.", "Good line. Keep A. Keep B.")
    assert out == "Good line. Keep A. Keep B."


def test_trim_is_case_and_whitespace_insensitive():
    draft = "Bad line. The only sound was the wind."
    out, _ = _patch(draft, "Bad line.", "Good line.  the   ONLY sound was the wind.")
    assert out == "Good line. The only sound was the wind."


def test_trim_preserves_paragraph_structure_inside_the_replacement():
    draft = "One.\n\nTwo bad.\n\nThree."
    out, _ = _patch(draft, "Two bad.", "Two good.\n\nExtra para. Three.")
    assert out == "One.\n\nTwo good.\n\nExtra para.\n\nThree."


def test_outer_whitespace_on_a_replacement_is_stripped():
    draft = "Alpha one. Beta two. Gamma three."
    out, _ = _patch(draft, "Beta two.", "   Delta four.\n ")
    assert out == "Alpha one. Delta four. Gamma three."


def test_dangling_emphasis_marker_does_not_hide_the_repeat():
    # The target span excludes the `*` wrapping it, so the draft to the left ends
    # in a marker fragment. It must not read as a word between the two copies.
    draft = '"I\'m bored." *She murmured.*'
    out, errors = _patch(draft, "She murmured.", '"I\'m bored."')
    assert out == draft
    assert len(errors) == 1


# ── What healing must leave alone ─────────────────────────────────────────────


def test_a_merely_similar_neighbour_is_not_trimmed():
    # difflib rates these 0.95 similar; anything fuzzy would delete the model's work.
    draft = "He nodded. She nodded."
    out, errors = _patch(draft, "He nodded.", "He shrugged.")
    assert out == "He shrugged. She nodded."
    assert errors == []


def test_only_the_adjacent_sentence_counts():
    draft = "Yes. Filler here. Bad one. Tail."
    out, errors = _patch(draft, "Bad one.", "Yes.")
    assert out == "Yes. Filler here. Yes. Tail."
    assert errors == []


def test_differing_terminator_is_a_different_sentence():
    # Healing trims only an *unchanged* copy, because only an unchanged copy is
    # guaranteed to rejoin: `howled!` beside `howled.` is text the model may
    # have meant, so healing leaves it alone and hands the whole replacement on.
    draft = "Bad line. The wind howled."
    healed = heal_replacement(draft, 0, len("Bad line."), "Good line. The wind howled!")
    assert healed.replace == "Good line. The wind howled!"
    assert healed.notes == ()


def test_a_repunctuated_copy_of_the_next_sentence_is_rejected_by_the_guard():
    # The other half of the split above: what healing declines to trim, the
    # protected-sequence guard refuses to splice. It compares lexically, where
    # `howled!` and `howled.` are the same three words — so the duplication the
    # trim could not safely remove never reaches the draft. See
    # tests/unit/test_protected_sequences.py.
    draft = "Bad line. The wind howled."
    out, errors = _patch(draft, "Bad line.", "Good line. The wind howled!")
    assert out == draft
    assert errors == [
        "Error: the patch for id 1 copies protected text from after the flagged span "
        "\u2014 \u201cThe wind howled\u201d is already in the draft; replace only the flagged text."
    ]


def test_an_overlap_that_is_not_end_aligned_is_not_trimmed():
    # "the door" is shared, but it sits inside the replacement rather than
    # against the span's edge, so trimming it could not rejoin into one line.
    draft = "He hated the door. It stayed shut."
    out, errors = _patch(draft, "He hated the door.", "The door mocked him.")
    assert out == "The door mocked him. It stayed shut."
    assert errors == []


def test_the_trim_never_cuts_inside_a_word():
    # A shared prefix of a longer word is not a copy of anything: comparison is
    # whole words, so "dog" against "dogged" is a miss, not a three-char trim.
    draft = "She whistled for the dog dogged by the rain."
    out, errors = _patch(draft, "dogged by the rain.", "dogged along beside her.")
    assert out == "She whistled for the dog dogged along beside her."
    assert errors == []


def test_a_clean_replacement_is_untouched():
    draft = "Alpha one. Beta two. Gamma three."
    out, errors = _patch(draft, "Beta two.", "Delta four.")
    assert out == "Alpha one. Delta four. Gamma three."
    assert errors == []


# ── Deliberate deletion closes its own seam ───────────────────────────────────


@pytest.mark.parametrize(
    ("draft", "span", "expected"),
    [
        ("Alpha one. Beta two. Gamma three.", "Beta two.", "Alpha one. Gamma three."),
        ("Alpha one. Beta two.", "Beta two.", "Alpha one."),
        ("Alpha one. Beta two.", "Alpha one.", "Beta two."),
        ("A.\n\nB.\n\nC.", "B.", "A.\n\nC."),
        ("A. B.\n\nC.", "B.", "A.\n\nC."),
        ("A.\n\nB. C.", "B.", "A.\n\nC."),
    ],
)
def test_empty_replace_deletes_the_span_without_stranding_whitespace(draft, span, expected):
    out, errors = _patch(draft, span, "")
    assert out == expected
    assert errors == []


@pytest.mark.parametrize(
    ("draft", "expected"),
    [
        ("A.\nB.\nC.", "A.\nC."),
        ("A. B.\nC.", "A.\nC."),
        ("A.\nB. C.", "A.\nC."),
    ],
)
def test_deleting_a_line_does_not_promote_the_seam_to_a_paragraph_break(draft, expected):
    # The two single newlines around a deleted line of dialogue spell "\n\n" when
    # concatenated. Measuring each side separately is what keeps the strongest
    # break the draft *already had* rather than inventing a paragraph split.
    out, errors = _patch(draft, "B.", "")
    assert out == expected
    assert errors == []


# ── Interaction with the rest of apply_id_patches ─────────────────────────────


def test_a_replacement_that_heals_down_to_the_flagged_text_is_a_no_op():
    # "Bad. C." over "Bad." is the model restating its target and appending the
    # sentence after it. Trimming the copy leaves the flagged text itself, which
    # edits nothing — it must cost an error, not count as an applied patch.
    draft = "Alpha one. Bad. Gamma three."
    out, errors = _patch(draft, "Bad.", "Bad. Gamma three.")
    assert out == draft
    assert errors == ["Error: the patch for id 1 is a no-op — `replace` repeats the flagged text unchanged."]


def test_a_healed_away_patch_still_costs_exactly_one_error():
    # Document mode counts applications as len(patches) - len(errors).
    draft = '"I\'m bored." She murmured. Gamma three.'
    targets = [
        Target(tid=1, span="She murmured.", start=13, end=26),
        Target(tid=2, span="Gamma three.", start=27, end=39),
    ]
    patches = [{"id": 1, "replace": '"I\'m bored."'}, {"id": 2, "replace": "Delta four."}]
    out, errors = apply_id_patches(draft, targets, patches)
    assert out == '"I\'m bored." She murmured. Delta four.'
    assert len(patches) - len(errors) == 1


def test_healing_sees_the_already_patched_tail():
    # Splicing runs back-to-front, so the later patch is final by the time the
    # earlier one is healed — it must be compared against the new text, not the old.
    draft = "Alpha one. Beta two. Gamma three."
    targets = [
        Target(tid=1, span="Beta two.", start=11, end=20),
        Target(tid=2, span="Gamma three.", start=21, end=33),
    ]
    out, errors = apply_id_patches(
        draft,
        targets,
        [{"id": 1, "replace": "Delta four. Omega five."}, {"id": 2, "replace": "Omega five."}],
    )
    assert out == "Alpha one. Delta four. Omega five."
    assert errors == []


def test_heal_rejections_are_reported_in_document_order():
    draft = "Alpha one. Alpha one. Beta two. Beta two."
    targets = [
        Target(tid=1, span="Alpha one.", start=11, end=21),
        Target(tid=2, span="Beta two.", start=32, end=41),
    ]
    _, errors = apply_id_patches(
        draft,
        targets,
        [{"id": 2, "replace": "Beta two."}, {"id": 1, "replace": "Alpha one."}],
    )
    # Both are no-ops against their own span, caught before healing; the ordering
    # guarantee is asserted with two genuine heals instead.
    assert len(errors) == 2

    _, healed_errors = apply_id_patches(
        draft,
        targets,
        [{"id": 2, "replace": "beta two."}, {"id": 1, "replace": "alpha one."}],
    )
    assert healed_errors == [
        "Error: the patch for id 1 only repeats text that already surrounds the flagged span "
        "— send new prose for the flagged text itself.",
        "Error: the patch for id 2 only repeats text that already surrounds the flagged span "
        "— send new prose for the flagged text itself.",
    ]


# ── Unit-level contract ───────────────────────────────────────────────────────


def test_heal_replacement_reports_what_it_did():
    draft = "Alpha one. Beta two. Gamma three."
    healed = heal_replacement(draft, 11, 20, "Alpha one. Delta four. Gamma three.")
    assert healed.replace == "Delta four."
    assert healed.rejection is None
    assert len(healed.notes) == 2


def test_heal_replacement_widens_the_span_for_a_deletion():
    draft = "Alpha one. Beta two. Gamma three."
    # Both separators are absorbed and one is re-emitted, so the deletion cannot
    # leave the doubled space a bare splice at [11:20] would.
    healed = heal_replacement(draft, 11, 20, "")
    assert (healed.start, healed.end, healed.replace) == (10, 21, " ")
