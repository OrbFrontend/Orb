"""Integration of the negated-narration detector with the audit, targets,
patching, serializers, Editor prompts, and Document-mode exclusion."""

from __future__ import annotations

import pytest

from backend.analysis import (
    AUDIT_TYPES,
    StaleSourceError,
    apply_id_patches,
    build_targets,
    filter_audit_report_to_text,
    format_numbered_report,
    format_report,
    report_to_dict,
    run_audit,
)
from backend.features.documents.audit import _audit_sync, audit_document
from backend.pipeline.passes.editor.prompts import (
    PATCH_CATEGORY_RULES,
    build_editor_prompt,
    patch_instructions,
)

_ON = {key: key == "negated_narration" for key in AUDIT_TYPES}
_RULE = PATCH_CATEGORY_RULES["negated_narration"]

# Two separate beats: a cascade with a payoff, then a null reaction.
_DRAFT = "She doesn't jump. Doesn't gasp. She just slowly straightens up.\n\nThe fire pops. He didn't answer."


def _audit(draft: str, **kwargs):
    return run_audit(draft, [], audit_toggles=kwargs.pop("toggles", _ON), structural_text=draft, **kwargs)


# ── Audit wiring and toggles ──────────────────────────────────────────────────


def test_findings_count_as_issues():
    report = _audit(_DRAFT)
    assert report.negation_result is not None
    assert [f.kinds for f in report.negation_findings] == [["cascade", "pivot"], ["null_reaction"]]
    assert report.total_issues == 2
    assert not report.is_clean


def test_below_gate_counters_do_not_count_as_issues():
    report = _audit("He didn't answer. She sat down.")
    assert report.negation_result is not None and report.negation_result.raw_hits == 1
    assert report.is_clean
    assert report.total_issues == 0


def test_history_cannot_change_gate_style_or_counters():
    history = ["*Nobody moved. Nothing happened.* She did not speak. It wasn't a question."] * 3
    draft = "He didn't answer. She sat down by the fire and warmed her hands."
    full = "\n\n".join([*history, draft])
    with_history = run_audit(full, [], assistant_messages=history, structural_text=draft, audit_toggles=_ON)
    alone = _audit(draft)
    a, b = with_history.negation_result, alone.negation_result
    assert a is not None and b is not None
    assert (a.raw_hits, a.narration_sentences, a.negated_sentences, a.style) == (
        b.raw_hits,
        b.narration_sentences,
        b.negated_sentences,
        b.style,
    )
    assert a.findings == b.findings == []
    assert a.source_text == draft


def test_default_is_off_for_missing_key_and_missing_map():
    assert run_audit(_DRAFT, []).negation_result is None
    assert run_audit(_DRAFT, [], audit_toggles={"banned_phrases": True}).negation_result is None
    assert run_audit(_DRAFT, [], audit_toggles={"negated_narration": False}).negation_result is None
    assert run_audit(_DRAFT, [], audit_toggles={"negated_narration": True}).negation_result is not None


def test_existing_categories_keep_missing_key_defaults():
    report = run_audit(
        "The tension in the air was palpable. The tension in the air grew.",
        [["tension in the air"]],
        audit_toggles={"negated_narration": True},
    )
    assert report.cliche_result.flagged_count > 0


def test_min_hits_is_exposed():
    report = _audit("He didn't answer. She sat down.", negation_min_hits=0)
    assert [f.kinds for f in report.negation_findings] == [["null_reaction"]]


# ── Source-aware filtering ────────────────────────────────────────────────────


def test_filter_preserves_findings_for_the_same_draft():
    report = _audit(_DRAFT)
    filtered = filter_audit_report_to_text(report, _DRAFT)
    assert filtered.negation_result is report.negation_result


def test_filter_rejects_a_stale_source_instead_of_relocating():
    report = _audit(_DRAFT)
    with pytest.raises(StaleSourceError):
        filter_audit_report_to_text(report, "Preface.\n\n" + _DRAFT)
    with pytest.raises(StaleSourceError):
        build_targets(report, _DRAFT + " ")


def test_mismatched_span_is_rejected():
    report = _audit(_DRAFT)
    assert report.negation_result is not None
    report.negation_result.findings[0].span = "tampered"
    with pytest.raises(StaleSourceError):
        filter_audit_report_to_text(report, _DRAFT)


# ── Anchored targets ──────────────────────────────────────────────────────────


def test_targets_are_anchored_and_numbered_with_reasons():
    targets = build_targets(_audit(_DRAFT), _DRAFT)
    assert [(t.start, t.span) for t in targets] == [
        (0, "She doesn't jump. Doesn't gasp. She just slowly straightens up."),
        (_DRAFT.index("He didn't"), "He didn't answer."),
    ]
    assert targets[0].categories == ["negated_narration"]
    assert targets[0].reasons == [
        'narrates what doesn\'t happen: 2 consecutive denials, before the payoff "She just slowly straightens…"'
    ]
    assert targets[1].reasons == ["narrates what doesn't happen"]
    report_text = format_numbered_report(targets)
    assert "[1] She doesn't jump." in report_text


def test_every_shape_kind_has_a_reason():
    draft = "He didn't answer.\n\nShe giggles, not pulling back."
    targets = build_targets(_audit(draft), draft)
    assert [t.reasons for t in targets] == [
        ["narrates what doesn't happen"],
        ["narrates what doesn't happen: tags a denied action onto the sentence"],
    ]


def test_outer_markers_trim_within_the_interval_and_inner_markers_stay():
    draft = "*She doesn't move.* *Doesn't breathe.* Why would you ask? *She just stares.*\n\n*Nobody speaks.*"
    report = _audit(draft)
    targets = build_targets(report, draft)
    assert [t.span for t in targets] == ["She doesn't move.* *Doesn't breathe.", "Nobody speaks."]
    for t in targets:
        assert draft[t.start : t.end] == t.span


def test_repeated_text_inside_an_earlier_larger_finding_keeps_its_own_location():
    beat = "He didn't answer."
    draft = f"Nobody moved. {beat}\n\nThe fire popped. {beat}"
    targets = build_targets(_audit(draft), draft)
    assert [(t.start, t.end) for t in targets] == [(0, len(f"Nobody moved. {beat}")), (draft.rindex(beat), len(draft))]
    assert targets[1].n_occurrences == 1


def test_repeated_text_in_an_earlier_thought_is_not_targeted():
    draft = "*He didn't answer.*\n\nShe waits. He didn't answer. Nobody spoke. He just stared."
    targets = build_targets(_audit(draft), draft)
    assert all(t.start > draft.index("\n\n") for t in targets)


def test_overlap_with_contrastive_negation_is_one_target_with_both_reasons():
    draft = "She doesn't answer. Not a request, but an order.\n\nHe didn't move."
    toggles = {**_ON, "contrastive_negation": True}
    report = _audit(draft, toggles=toggles)
    assert report.not_but_result, "fixture must also trip contrastive negation"
    targets = build_targets(report, draft)
    both = [t for t in targets if len(t.categories) == 2]
    assert len(both) == 1
    assert set(both[0].categories) == {"contrastive_negation", "negated_narration"}
    # Both findings count, one target is patched.
    assert report.total_issues == len(report.not_but_result) + len(report.negation_findings)


# ── Serializers ───────────────────────────────────────────────────────────────


def test_report_to_dict_section_ids_point_at_the_right_occurrence():
    beat = "He didn't answer."
    draft = f"Nobody moved. {beat}\n\nThe fire popped. {beat}"
    report = _audit(draft)
    out = report_to_dict(report, draft)
    section = out["sections"]["negated_narration"]
    assert [entry["kinds"] for entry in section] == [["cascade"], ["null_reaction"]]
    assert [entry["span"] for entry in section] == [f"Nobody moved. {beat}", beat]
    assert [entry["ids"] for entry in section] == [[1], [2]]
    assert "source_text" not in str(out) and "start" not in section[0]
    assert "ids" not in report_to_dict(report)["sections"]["negated_narration"][0]


def test_format_report_has_a_negated_narration_section():
    text = format_report(_audit(_DRAFT))
    assert "Negated Narration" in text
    assert "2 consecutive denials" in text


# ── Patching ──────────────────────────────────────────────────────────────────


def test_multi_sentence_target_is_patched_once_with_surroundings_intact():
    draft = "Rain taps the glass. *She doesn't move.* *Doesn't breathe.* \"Hey.\" *Nobody answers.*\n\nEnd."
    report = _audit(draft)
    targets = build_targets(report, draft)
    first = targets[0]
    assert first.span == "She doesn't move.* *Doesn't breathe."
    out, errors = apply_id_patches(draft, targets, [{"id": first.tid, "replace": "She holds perfectly still."}])
    assert errors == []
    assert out == 'Rain taps the glass. *She holds perfectly still.* "Hey." *Nobody answers.*\n\nEnd.'


def test_unchanged_patch_is_a_no_op_error():
    targets = build_targets(_audit(_DRAFT), _DRAFT)
    out, errors = apply_id_patches(_DRAFT, targets, [{"id": 1, "replace": targets[0].span}])
    assert out == _DRAFT
    assert [e.kind for e in errors] == ["no_op"]


# ── Document mode never runs it ───────────────────────────────────────────────


def test_document_audit_excludes_negated_narration_even_when_enabled():
    report = _audit_sync(_DRAFT, "", [], {"negated_narration": True})
    assert report.negation_result is None


async def test_document_audit_payload_has_no_negated_narration_section():
    res = await audit_document(_DRAFT, "", [], {"negated_narration": True}, assisted=False, truncated=False)
    assert "negated_narration" not in res["report"]["sections"]


# ── Editor prompts ────────────────────────────────────────────────────────────


def test_patch_prompt_carries_the_rule_and_span_wording():
    prompt = build_editor_prompt(True, "REPORT", False, "", patch_categories={"negated_narration"})
    assert _RULE in prompt
    assert "the new text for that span" in prompt
    assert patch_instructions({"negated_narration"}, shown=set()) == f"- {_RULE}"


def test_rewrite_prompt_carries_the_content_rule_without_patch_instructions():
    prompt = build_editor_prompt(True, "REPORT", True, "LENGTH", patch_categories={"negated_narration"})
    assert _RULE in prompt
    assert "editor_apply_patch" not in prompt
    assert "PATCHING RULES" not in prompt
    assert prompt.index("REPORT") < prompt.index(_RULE) < prompt.index("LENGTH")


def test_rewrite_prompt_is_unchanged_for_other_categories():
    with_other = build_editor_prompt(True, "REPORT", True, "LENGTH", patch_categories={"banned_phrases"})
    without = build_editor_prompt(True, "REPORT", True, "LENGTH")
    assert with_other == without
