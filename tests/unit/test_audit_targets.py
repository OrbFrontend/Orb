"""Unit tests for analysis/targets.py — the id-addressable target table.

Covers the three things the design gate said had to be right before the ID
method could replace string search: occurrence resolution against the narration
mask, region merging across detectors that segment differently, and duplicate
labelling that never promises an id it did not emit.
"""

from __future__ import annotations

from backend.analysis import (
    AuditReport,
    build_targets,
    format_numbered_report,
    target_ids_for,
)
from backend.analysis.detectors.anti_echo import EchoResult, FlaggedEcho
from backend.analysis.detectors.opening_monotony import FlaggedOpener, MonotonyResult
from backend.analysis.detectors.phrase_repetition import FlaggedPhrase, PhraseResult
from backend.analysis.detectors.slop_detector import (
    ClicheHit,
    DetectionResult,
    FlaggedSentence,
)
from backend.analysis.detectors.structural_repetition import StructuralResult


def _report() -> AuditReport:
    return AuditReport.clean()


def _cliches(*sentences: str) -> DetectionResult:
    flagged = [FlaggedSentence(s, [ClicheHit(f"phrase-{i}", 0.9)]) for i, s in enumerate(sentences)]
    return DetectionResult(flagged_sentences=flagged, unique_cliches=[], total_sentences=8, flagged_count=len(flagged))


# ── Ordering, merging of reasons on one span ──────────────────────────────────


def test_targets_are_document_ordered_and_numbered_from_one():
    draft = "Alpha one. Beta two. Gamma three."
    r = _report()
    r.cliche_result = _cliches("Gamma three.", "Alpha one.")
    targets = build_targets(r, draft)
    assert [t.tid for t in targets] == [1, 2]
    assert [t.span for t in targets] == ["Alpha one.", "Gamma three."]
    assert [(t.start, t.end) for t in targets] == [(0, 10), (21, 33)]


def test_two_detectors_on_one_sentence_make_one_target_with_both_reasons():
    draft = "She waited. She watched the door. She said nothing."
    r = _report()
    r.cliche_result = _cliches("She watched the door.")
    r.not_but_result = [{"sentence": "She watched the door.", "is_parallel": False}]
    targets = build_targets(r, draft)
    assert len(targets) == 1
    assert len(targets[0].reasons) == 2
    assert set(targets[0].categories) == {"banned_phrases", "contrastive_negation"}


# ── The overlap defect the gate flagged ───────────────────────────────────────


def test_overlapping_regions_merge_into_one_target():
    # contrastive_negation keeps dialogue inline; openers strip it. One sentence,
    # two spans, one containing the other — two ids here would make splicing
    # back-to-front overwrite the inner patch with a stale `end`.
    draft = 'He turned. "Stay," he said, and it was not a plea but a demand. She stayed.'
    outer = '"Stay," he said, and it was not a plea but a demand.'
    inner = "he said, and it was not a plea but a demand."
    assert inner in draft and outer in draft
    r = _report()
    r.not_but_result = [{"sentence": outer, "is_parallel": False}]
    r.monotony_result = MonotonyResult(
        flagged_openers=[FlaggedOpener("he", 2, 2, 0.4, ["he waited.", inner])],
        all_openers={},
        total_sentences=6,
        monotony_score=0.4,
    )
    targets = build_targets(r, draft)
    assert len(targets) == 1
    merged = targets[0]
    assert draft[merged.start : merged.end] == merged.span
    assert inner in merged.span
    assert set(merged.categories) == {"contrastive_negation", "repetitive_openers"}


def test_merged_target_splices_without_losing_the_inner_edit():
    from backend.analysis import apply_id_patches

    draft = 'He turned. "Stay," he said, and it was not a plea but a demand. She stayed.'
    r = _report()
    r.not_but_result = [{"sentence": '"Stay," he said, and it was not a plea but a demand.', "is_parallel": False}]
    r.monotony_result = MonotonyResult(
        flagged_openers=[FlaggedOpener("he", 2, 2, 0.4, ["he waited.", "he said, and it was not a plea but a demand."])],
        all_openers={},
        total_sentences=6,
        monotony_score=0.4,
    )
    targets = build_targets(r, draft)
    out, errors = apply_id_patches(draft, targets, [{"id": 1, "replace": "REWRITTEN"}])
    assert errors == []
    # One splice covering both findings. The draft's opening `"` survives — it sat
    # outside the marker-stripped span, same as under the old marker-core path.
    # Two ids here would have replaced the inner span and then overwritten it
    # using the outer span's now-stale `end`, losing the edit silently.
    assert out == 'He turned. "REWRITTEN She stayed.'
    assert "not a plea but a demand" not in out


def test_adjacent_non_overlapping_targets_stay_separate():
    draft = "Alpha one. Beta two."
    r = _report()
    r.cliche_result = _cliches("Alpha one.", "Beta two.")
    targets = build_targets(r, draft)
    assert len(targets) == 2
    assert targets[0].end <= targets[1].start


# ── Duplicates ────────────────────────────────────────────────────────────────


def test_duplicate_span_flagged_twice_gets_one_id_per_copy():
    draft = "The air was thick. Nothing moved. The air was thick."
    r = _report()
    r.cliche_result = _cliches("The air was thick.", "The air was thick.")
    targets = build_targets(r, draft)
    assert [t.tid for t in targets] == [1, 2]
    assert [t.start for t in targets] == [0, 34]
    assert [(t.occurrence, t.n_occurrences) for t in targets] == [(0, 2), (1, 2)]
    assert all(t.is_duplicate for t in targets)
    report_text = format_numbered_report(targets)
    assert "identical copy 1 of 2" in report_text
    assert "identical copy 2 of 2" in report_text


def test_phrase_repetition_targets_the_draft_copy_not_an_earlier_message():
    # example_sentences runs oldest-first with the draft last, and both report
    # renderings show the last one. An earlier message's sentence that is also a
    # substring of the draft must not win the anchor, or report_to_dict asks for
    # ids on a sentence no target covers and the panel shows a finding it cannot
    # number.
    draft = "The air was thick. Nothing moved at all."
    r = _report()
    r.phrase_result = PhraseResult(
        flagged_phrases=[FlaggedPhrase("air was thick", 2, [0, 1], ["The air was thick.", "Nothing moved at all."])],
        total_messages=2,
    )
    targets = build_targets(r, draft)
    assert [t.span for t in targets] == ["Nothing moved at all."]
    assert target_ids_for(targets, "Nothing moved at all.") == [1]


def test_duplicate_reported_once_is_not_labelled_as_two_copies():
    # phrase_repetition emits one example sentence per phrase, so a span that
    # occurs twice still yields exactly one id. The report must not advertise a
    # second occurrence the model cannot address.
    draft = "The air was thick. Nothing moved. The air was thick."
    r = _report()
    r.phrase_result = PhraseResult(
        flagged_phrases=[FlaggedPhrase("air was thick", 3, [0, 1], ["The air was thick."])],
        total_messages=4,
    )
    targets = build_targets(r, draft)
    assert len(targets) == 1
    assert not targets[0].is_duplicate
    assert "identical copy" not in format_numbered_report(targets)


def test_duplicate_inside_dialogue_does_not_consume_an_occurrence_slot():
    # The narration copy is what the detector flagged; the quoted copy must not
    # shift the k-th-occurrence mapping onto the wrong offset.
    draft = '"The air was thick," he quoted. The air was thick.'
    r = _report()
    r.cliche_result = _cliches("The air was thick.")
    targets = build_targets(r, draft)
    assert len(targets) == 1
    assert targets[0].start == draft.rindex("The air was thick.")


def test_anti_echo_falls_back_to_dialogue_when_narration_has_no_copy():
    draft = 'She tilted her head. "You really think so?"'
    r = _report()
    r.echo_result = EchoResult(flagged_echoes=[FlaggedEcho("You really think so?", "really think so", 2)])
    targets = build_targets(r, draft)
    assert len(targets) == 1
    assert targets[0].span == "You really think so?"


# ── Findings with no addressable span ─────────────────────────────────────────


def test_structural_repetition_yields_no_targets():
    r = _report()
    r.structural_repetition_result = StructuralResult(
        is_repetitive=True,
        min_similarity=0.9,
        mean_similarity=0.9,
        shared_skeleton=["NARRATION", "SPEECH"],
        messages=[],
    )
    assert build_targets(r, "Some draft text.") == []
    assert not r.is_clean


def test_finding_absent_from_the_draft_is_dropped():
    r = _report()
    r.cliche_result = _cliches("A sentence the draft does not contain.")
    assert build_targets(r, "Alpha one. Beta two.") == []


def test_empty_targets_render_as_the_clean_report():
    assert "All checks passed" in format_numbered_report([])


# ── Report rendering + the panel bridge ───────────────────────────────────────


def test_numbered_report_carries_ids_reasons_and_marker_free_snippets():
    draft = "*Alpha one.* Beta two."
    r = _report()
    r.cliche_result = _cliches("*Alpha one.*")
    text = format_numbered_report(build_targets(r, draft))
    assert "[1] Alpha one." in text
    assert "contains banned phrase(s)" in text
    assert "patch each by its [id]" in text.lower()


def test_target_ids_for_finds_the_merged_target_containing_a_snippet():
    draft = 'He turned. "Stay," he said, and it was not a plea but a demand. She stayed.'
    r = _report()
    r.not_but_result = [{"sentence": '"Stay," he said, and it was not a plea but a demand.', "is_parallel": False}]
    r.monotony_result = MonotonyResult(
        flagged_openers=[FlaggedOpener("he", 2, 2, 0.4, ["he waited.", "he said, and it was not a plea but a demand."])],
        all_openers={},
        total_sentences=6,
        monotony_score=0.4,
    )
    targets = build_targets(r, draft)
    assert target_ids_for(targets, "he said, and it was not a plea but a demand.") == [1]
    assert target_ids_for(targets, "She stayed.") == []
