"""The protected-sequence guard: a replacement may not clone its neighbours.

Orb's exact-offset splice already guarantees that every byte outside the target
spans survives — ``P0 + R1 + P1 + … + Rn + Pn``. The invariant these tests pin
is the missing one: a replacement must not *copy* a significant run out of a
protected ``P`` region, which prints that text twice where no trim could safely
remove it.

The guard is deliberately format-agnostic, so the same lexical clone is tested
through straight quotes, smart quotes, asterisk roleplay, unquoted lines, and
paragraph breaks — one draft family whose target spans are byte-identical in
every rendering, which makes markup a clean controlled variable. The other half
of the contract is the false-positive half: short runs, common runs, text the
target already contained, and text belonging to another *mutable* target must
all still apply.
"""

from __future__ import annotations

import pytest

from backend.analysis import Target, apply_id_patches
from backend.analysis.guarding import guard_protected_sequences, protected_bands

# One two-target draft in five renderings. The target spans below are
# byte-identical in all of them, so any difference in outcome is markup alone.
DRAFTS = {
    "straight": '"Don\'t touch it," Mara said. She said softly, her voice thick with tension. '
    '"I wasn\'t going to," Ilya replied. The silence was deafening.',
    "smart": "“Don’t touch it,” Mara said. She said softly, her voice thick with tension. "
    "“I wasn’t going to,” Ilya replied. The silence was deafening.",
    "asterisk": "*Don't touch it,* Mara said. She said softly, her voice thick with tension. "
    "*I wasn't going to,* Ilya replied. The silence was deafening.",
    "plain": "Don't touch it, Mara said. She said softly, her voice thick with tension. "
    "I wasn't going to, Ilya replied. The silence was deafening.",
    "multi_para": '"Don\'t touch it," Mara said.\n\nShe said softly, her voice thick with tension.\n\n'
    '"I wasn\'t going to," Ilya replied.\n\nThe silence was deafening.',
}

NARRATION = "She said softly, her voice thick with tension."
CLOSER = "The silence was deafening."


def _targets(draft: str) -> list[Target]:
    """The two flagged narration spans, numbered in document order."""
    out = []
    for tid, span in enumerate((NARRATION, CLOSER), start=1):
        start = draft.index(span)
        out.append(Target(tid=tid, span=span, start=start, end=start + len(span)))
    return out


def _apply(draft: str, patches: list[dict]) -> tuple[str, list[str]]:
    return apply_id_patches(draft, _targets(draft), patches)


def _rejection(errors: list[str]) -> str:
    assert len(errors) == 1, errors
    return errors[0]


# ── The clone, in every markup the corpus can wear ────────────────────────────


@pytest.mark.parametrize("markup", list(DRAFTS))
def test_a_copied_line_is_rejected_whatever_wraps_it(markup):
    # `"Don't touch it,"` sits in protected text before the flagged narration.
    # Quotes, smart quotes, asterisks, a bare line and a paragraph break are all
    # formatting: the lexical run don't/touch/it is the same copy in each.
    draft = DRAFTS[markup]
    out, errors = _apply(draft, [{"id": 1, "replace": "Don't touch it, she whispered again."}])
    assert out == draft
    assert "copies protected text from before the flagged span" in _rejection(errors)


def test_the_rejection_quotes_the_draft_not_the_folded_key():
    # The model is told which text it copied, in the draft's own spelling, so a
    # smart-quoted draft is quoted back with its own apostrophe.
    _, errors = _apply(DRAFTS["smart"], [{"id": 1, "replace": "Don't touch it, she whispered again."}])
    assert "“Don’t touch it”" in _rejection(errors)


def test_changing_the_wrapper_does_not_smuggle_a_copy_through():
    # Quoted in the draft, unquoted in the replacement. A guard keyed on quote
    # syntax — the one the eval harness shipped with — misses exactly this.
    out, errors = _apply(DRAFTS["straight"], [{"id": 1, "replace": "She repeated it: don't touch it."}])
    assert out == DRAFTS["straight"]
    assert _rejection(errors).startswith("Error: the patch for id 1 copies protected text")


def test_a_copy_from_after_the_span_names_the_right_side():
    # Both adjacent gaps are inspected, and the error says which one was copied
    # so the model knows which direction it over-reached in.
    out, errors = _apply(DRAFTS["straight"], [{"id": 1, "replace": "I wasn't going to, she thought."}])
    assert out == DRAFTS["straight"]
    assert "from after the flagged span" in _rejection(errors)


# ── Rejection, not repair ─────────────────────────────────────────────────────


def test_an_interior_copy_is_rejected_rather_than_trimmed():
    # An end-aligned copy can be trimmed because the remainder rejoins the text
    # it duplicated. An interior one carries no such guarantee, so the writer's
    # original text is kept instead of a guess at what the model meant.
    draft = DRAFTS["straight"]
    out, errors = _apply(draft, [{"id": 1, "replace": "She said don't touch it, and her hand fell away."}])
    assert out == draft
    assert len(errors) == 1


def test_one_rejected_patch_costs_exactly_one_error():
    # Document mode counts applications as len(patches) - len(errors).
    patches = [{"id": 1, "replace": "Don't touch it, she whispered again."}, {"id": 2, "replace": "Nobody spoke."}]
    out, errors = _apply(DRAFTS["straight"], patches)
    assert len(patches) - len(errors) == 1
    assert out.endswith("Nobody spoke.")
    assert NARRATION in out  # the rejected target kept the writer's text


def test_healing_still_trims_an_end_aligned_copy():
    # The guard did not replace healing: a tail that repeats the draft ahead of
    # the span is still trimmed and the patch still lands.
    draft = DRAFTS["straight"]
    out, errors = _apply(draft, [{"id": 1, "replace": 'Mara warned him again. "I wasn\'t going to," Ilya replied.'}])
    assert errors == []
    assert out == draft.replace(NARRATION, "Mara warned him again.")


def test_the_guard_reads_the_healed_text_not_the_raw_replacement():
    # Healing trims the copied tail; what it leaves still contains an interior
    # clone. Guarding the raw `replace` would have found the tail first and
    # reported the wrong run — guarding the healed text finds the real one.
    draft = DRAFTS["straight"]
    out, errors = _apply(
        draft,
        [{"id": 1, "replace": "Don't touch it, she whispered. \"I wasn't going to,\" Ilya replied."}],
    )
    assert out == draft
    assert "“Don't touch it”" in _rejection(errors)


# ── What must still apply ─────────────────────────────────────────────────────


def test_text_from_another_target_is_mutable_not_protected():
    # The closer is itself flagged, so it is not protected context for its
    # neighbour: reusing its words is a prose judgment, not a clone.
    draft = DRAFTS["straight"]
    out, errors = _apply(draft, [{"id": 1, "replace": "The silence was deafening between them."}])
    assert errors == []
    assert out == draft.replace(NARRATION, "The silence was deafening between them.")


def test_a_sequence_the_target_already_contained_may_stay():
    # "the sealed door" is in the protected lead-in *and* in the flagged span.
    # It is the thing being edited, so keeping it is not copying it.
    draft = "Mara stared at the sealed door. The sealed door was heavy with dread. She waited."
    span = "The sealed door was heavy with dread."
    start = draft.index(span)
    targets = [Target(tid=1, span=span, start=start, end=start + len(span))]
    out, errors = apply_id_patches(draft, targets, [{"id": 1, "replace": "The sealed door creaked."}])
    assert errors == []
    assert out == "Mara stared at the sealed door. The sealed door creaked. She waited."


def test_short_overlaps_do_not_hard_fail():
    # One- and two-token overlaps are ambiguous without content classification;
    # rejecting them would flag every repeated name and dialogue tag.
    draft = DRAFTS["straight"]
    out, errors = _apply(draft, [{"id": 1, "replace": "Mara said nothing else."}])
    assert errors == []
    assert out == draft.replace(NARRATION, "Mara said nothing else.")


def test_a_short_three_token_run_is_logged_not_rejected(caplog):
    # Three tokens but only eight alphanumeric characters: over the token floor,
    # under the character floor. Allowed, and logged so the false-positive
    # corpus can be read before the constants move.
    draft = "He sat in the car. The rain was heavy and grey. She never looked back."
    span = "The rain was heavy and grey."
    start = draft.index(span)
    targets = [Target(tid=1, span=span, start=start, end=start + len(span))]
    with caplog.at_level("INFO", logger="backend.analysis.guarding"):
        out, errors = apply_id_patches(draft, targets, [{"id": 1, "replace": "Water pooled in the car park."}])
    assert errors == []
    assert "in the car" in out
    assert "allowing a 3-token match" in caplog.text


def test_a_run_repeated_in_the_local_context_is_not_unique_enough():
    # The same words on both sides of the span are the draft's own refrain, not
    # a run the model lifted from one identifiable place.
    draft = "The bell rang twice. Her heart hammered against her ribs. The bell rang twice."
    span = "Her heart hammered against her ribs."
    start = draft.index(span)
    targets = [Target(tid=1, span=span, start=start, end=start + len(span))]
    out, errors = apply_id_patches(draft, targets, [{"id": 1, "replace": "The bell rang twice, she thought."}])
    assert errors == []
    assert out == draft.replace(span, "The bell rang twice, she thought.")


def test_a_two_token_name_near_the_target_is_not_a_false_positive():
    draft = "Captain Ilyra crossed the deck. The night was dark and full of terrors. She waited below."
    span = "The night was dark and full of terrors."
    start = draft.index(span)
    targets = [Target(tid=1, span=span, start=start, end=start + len(span))]
    out, errors = apply_id_patches(draft, targets, [{"id": 1, "replace": "Captain Ilyra frowned at the water."}])
    assert errors == []
    assert "Captain Ilyra frowned" in out


def test_a_three_token_name_is_a_known_false_positive():
    # Locality, uniqueness and the length floors reduce this risk but cannot
    # remove it: a three-word name repeated beside its own mention reads exactly
    # like a clone. Pinned as the measured cost of the conservative policy — the
    # fallback keeps the writer's text, which is the acceptable failure here.
    draft = "Captain Ilyra Venn crossed the deck. The night was dark and full of terrors. She waited below."
    span = "The night was dark and full of terrors."
    start = draft.index(span)
    targets = [Target(tid=1, span=span, start=start, end=start + len(span))]
    out, errors = apply_id_patches(draft, targets, [{"id": 1, "replace": "Captain Ilyra Venn frowned."}])
    assert out == draft
    assert "“Captain Ilyra Venn”" in _rejection(errors)


# ── Offsets under back-to-front application ───────────────────────────────────


def test_protected_gaps_survive_a_reordered_multi_patch_call():
    # The later patch changes the draft's length before the earlier one is
    # healed and guarded. Bands are cut from the original draft at offsets that
    # are still valid there, so the verdict must not depend on patch order.
    draft = DRAFTS["straight"]
    patches = [
        {"id": 2, "replace": "Nobody spoke for a long, long while afterwards."},
        {"id": 1, "replace": "Don't touch it, she whispered again."},
    ]
    forward, forward_errors = _apply(draft, patches)
    reverse, reverse_errors = _apply(draft, list(reversed(patches)))
    assert forward == reverse
    assert forward_errors == reverse_errors
    assert NARRATION in forward  # rejected
    assert "Nobody spoke for a long, long while afterwards." in forward  # applied
    assert "from before the flagged span" in _rejection(forward_errors)


def test_a_gap_two_targets_away_is_not_inspected():
    # Intended behaviour, pinned so that widening the bands is a deliberate
    # change: only the gaps adjacent to a target can be sliced from the original
    # draft and still be the text that will surround the replacement.
    draft = DRAFTS["straight"] + " The lantern guttered out on its hook."
    out, errors = _apply(draft, [{"id": 1, "replace": "The lantern guttered out on its hook, unnoticed."}])
    assert errors == []
    assert out.count("The lantern guttered out on its hook") == 2


def test_protected_bands_require_boundaries_that_bracket_the_target():
    # The signature carries most of the invariant — there is no argument that
    # names a gap further away — and the assertion carries the rest: boundaries
    # that do not bracket the target are not the text touching it.
    draft = DRAFTS["straight"]
    start = draft.index(NARRATION)
    with pytest.raises(AssertionError):
        protected_bands(draft, previous_end=0, start=start, end=start + len(NARRATION), next_start=start)


# ── The guard in isolation ────────────────────────────────────────────────────


def test_bands_are_trimmed_to_the_local_window():
    filler = " ".join(f"word{i}" for i in range(80))
    draft = f"Keep this phrase entirely. {filler} Flagged sentence here."
    span = "Flagged sentence here."
    start = draft.index(span)
    bands = protected_bands(draft, 0, start, start + len(span), len(draft))
    assert len(bands[0].tokens) == 32  # BAND_TOKENS
    # The opening line is outside the window, so copying it is not the guard's
    # business — that repeat is far enough away to read as the writer's own.
    assert guard_protected_sequences("Keep this phrase entirely, she said.", bands, span) is None


def test_a_replacement_shorter_than_the_floor_is_never_a_clone():
    draft = DRAFTS["straight"]
    start = draft.index(NARRATION)
    bands = protected_bands(draft, 0, start, start + len(NARRATION), draft.index(CLOSER))
    assert guard_protected_sequences("Mara said.", bands, NARRATION) is None
