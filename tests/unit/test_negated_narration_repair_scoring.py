"""Controls for the negated-narration repair scorer (scripts/bench/negated_narration_repair.py).

Scoring runs on the real target → patch path: ``build_targets`` and
``apply_id_patches_with_edits``, including healing and guards.
"""

from __future__ import annotations

from backend.analysis import AUDIT_TYPES, build_targets, run_audit
from backend.analysis.patching import apply_id_patches_with_edits
from scripts.bench.negated_narration_repair import repair_rate, score_repairs

_ON = {key: key == "negated_narration" for key in AUDIT_TYPES}


def _setup(draft: str):
    report = run_audit(draft, [], audit_toggles=_ON, structural_text=draft)
    assert report.negation_result is not None
    return report.negation_result.style, build_targets(report, draft)


def _score(draft: str, patches: list[dict]):
    style, targets = _setup(draft)
    edited, errors, edits = apply_id_patches_with_edits(draft, targets, patches)
    return score_repairs(draft, targets, edits, edited, style), errors, targets, edited


_PROSE = "She doesn't jump. Doesn't gasp. She just slowly straightens up.\n\nThe fire pops. He didn't answer."
_ASTERISK = "*She doesn't move.* *Doesn't breathe.* Why ask me? *She just stares.*\n\n*Nobody answers.*"


def test_unchanged_output_repairs_nothing():
    for draft in (_PROSE, _ASTERISK):
        style, targets = _setup(draft)
        scores = score_repairs(draft, targets, [], draft, style)
        assert len(scores) == len(targets) == 2
        assert [s.status for s in scores] == ["not_applied", "not_applied"]
        assert repair_rate(scores) == 0.0


def test_no_op_patch_is_not_a_repair():
    scores, errors, targets, edited = _score(_PROSE, [{"id": 2, "replace": "He didn't answer."}])
    assert edited == _PROSE
    assert [e.kind for e in errors] == ["no_op"]
    assert [s.status for s in scores] == ["not_applied", "not_applied"]


def test_faithful_rewrite_repairs_its_target_and_shifts_later_offsets():
    patches = [{"id": 1, "replace": "She slowly straightens up."}, {"id": 2, "replace": "He stares into the flames."}]
    scores, errors, _, edited = _score(_PROSE, patches)
    assert errors == []
    assert [s.status for s in scores] == ["repaired", "repaired"]
    second = scores[1].region
    assert second is not None and edited[second[0] : second[1]] == "He stares into the flames."


def test_residual_denial_in_the_patch_is_not_repaired():
    scores, _, _, _ = _score(_PROSE, [{"id": 2, "replace": "He did not reply."}])
    assert [s.status for s in scores] == ["not_applied", "residual"]


def test_singleton_and_partial_emphasis_block_targets():
    scores, errors, targets, edited = _score(_ASTERISK, [{"id": 1, "replace": "She holds still."}])
    assert errors == []
    assert targets[0].span == "She doesn't move.* *Doesn't breathe."
    assert edited.startswith("*She holds still.* Why ask me?")
    assert [s.status for s in scores] == ["repaired", "not_applied"]


def test_deletion_repairs_unless_it_forms_a_chain_across_the_seam():
    draft = "She waits by the door. Nobody moved. Nobody spoke. The rain keeps falling.\n\nHe didn't answer."
    scores, _, _, _ = _score(draft, [{"id": 1, "replace": ""}])
    assert [s.status for s in scores] == ["repaired", "not_applied"]

    # Deleting the stacked sentence leaves "isn't curious. It's accusing." —
    # a split contrast spanning the seam, so the deletion is not a repair.
    stacked = "The man by the door stood there, not moving, not breathing."
    seam = f"The question isn't curious. {stacked} It's accusing.\n\nHe didn't answer."
    style, targets = _setup(seam)
    assert targets[0].span == stacked
    edited, _, edits = apply_id_patches_with_edits(seam, targets, [{"id": 1, "replace": ""}])
    assert "curious. It's accusing." in edited
    assert [s.status for s in score_repairs(seam, targets, edits, edited, style)] == ["residual", "not_applied"]


def test_omitted_and_unknown_ids_stay_in_the_denominator():
    scores, errors, _, _ = _score(_PROSE, [{"id": 99, "replace": "x"}, {"id": 1, "replace": "She straightens."}])
    assert [e.kind for e in errors] == ["unknown_id"]
    assert [s.status for s in scores] == ["repaired", "not_applied"]
    assert repair_rate(scores) == 0.5


def test_restated_absence_is_flagged_for_review():
    scores, _, _, _ = _score(_PROSE, [{"id": 2, "replace": "He stays silent."}])
    assert scores[1].restated_absence
