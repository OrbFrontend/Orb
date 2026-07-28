"""The contributed-finding contract, exercised without an extension.

Phase 2 of the audit-detector feature is a value contract plus a merge, and
both are reachable by constructing :class:`ContributedFinding` directly. That is
the point of putting the type in ``analysis/``: the report, its filter, its two
renderers, and the editor's prefill path can all be tested before anything
compiles a package.
"""

from __future__ import annotations

from backend.analysis import (
    AuditReport,
    ContributedFinding,
    format_report,
    live_contributed_findings,
    report_to_dict,
)
from backend.analysis.detectors.slop_detector import ClicheHit, FlaggedSentence
from backend.analysis.patching import filter_audit_report_to_text
from backend.pipeline.passes.editor.editor import MAX_PREFILL_TARGETS, _prefill_targets


def _finding(snippet: str, note: str = "reads as filler", detector: str = "ext:slop") -> ContributedFinding:
    return ContributedFinding(detector_id=detector, label="Model-scored slop", snippet=snippet, note=note)


def _report(*findings: ContributedFinding) -> AuditReport:
    report = AuditReport.clean()
    report.contributed_results = findings
    return report


# ── the report field ─────────────────────────────────────────────────────────


def test_a_clean_report_has_no_contributed_findings():
    assert AuditReport.clean().contributed_results == ()
    assert AuditReport.clean().is_clean


def test_contributed_findings_count_toward_is_clean_and_total_issues():
    report = _report(_finding("She smiles softly."), _finding("A shiver runs down her spine."))
    assert not report.is_clean
    assert report.total_issues == 2


# ── filtering: what ages a finding out across the rewrite loop ───────────────


def test_a_stale_snippet_is_dropped_and_a_live_one_kept():
    report = _report(_finding("She smiles softly."), _finding("Gone after the rewrite."))
    filtered = filter_audit_report_to_text(report, "She smiles softly. And then left.")
    assert [f.snippet for f in filtered.contributed_results] == ["She smiles softly."]


def test_a_whole_draft_finding_survives_every_rewrite():
    """An empty snippet is a judgement about the draft, not about a span, so
    there is nothing for containment to invalidate."""
    report = _report(_finding("", note="the whole reply is too passive"))
    filtered = filter_audit_report_to_text(report, "completely different text")
    assert len(filtered.contributed_results) == 1


def test_live_contributed_findings_is_the_same_rule_the_report_filter_uses():
    findings = (_finding("kept"), _finding("dropped"))
    assert live_contributed_findings(findings, "... kept ...") == (findings[0],)


# ── rendering ────────────────────────────────────────────────────────────────


def test_format_report_groups_by_detector_under_the_host_stamped_label():
    text = format_report(
        _report(
            _finding("She smiles softly."),
            _finding("", note="too passive overall"),
            _finding("Other span.", detector="ext:other"),
        )
    )
    assert "Model-scored slop" in text
    assert '- "She smiles softly.": reads as filler' in text
    # No span: the note stands alone rather than rendering an empty quote.
    assert "- too passive overall" in text
    # Grouped, so the heading is not repeated for the second finding of a
    # detector but is emitted again for a different one.
    assert text.count("Model-scored slop") == 2


def test_report_to_dict_keys_contributed_sections_on_the_namespaced_id():
    body = report_to_dict(_report(_finding("She smiles softly.")))
    assert body["sections"]["ext:slop"] == [
        {"label": "Model-scored slop", "snippet": "She smiles softly.", "note": "reads as filler"}
    ]
    assert body["total_issues"] == 1
    assert body["is_clean"] is False


# ── the editor's prefill path ────────────────────────────────────────────────


def test_a_contributed_finding_reaches_prefill_targets():
    draft = "She smiles softly. Nothing else happens."
    targets = _prefill_targets(_report(_finding("She smiles softly.")), draft)
    assert targets == [("She smiles softly.", "reads as filler")]


def test_a_whole_draft_finding_produces_no_prefill_target():
    report = _report(_finding("", note="too passive overall"))
    assert _prefill_targets(report, "any draft") == []


def test_contributed_findings_are_not_starved_by_a_full_built_in_batch():
    """``_prefill_targets`` slices to MAX_PREFILL_TARGETS on *insertion* order
    before sorting by document position, so appending contributed findings last
    would let eight built-in findings crowd them out entirely."""
    sentences = [f"Sentence number {i} was written." for i in range(MAX_PREFILL_TARGETS)]
    draft = " ".join(sentences) + " She smiles softly."
    report = _report(_finding("She smiles softly."))
    report.cliche_result = type(report.cliche_result)(
        flagged_sentences=[
            FlaggedSentence(sentence=s, cliches=[ClicheHit(phrase="was written", score=1.0)]) for s in sentences
        ],
        unique_cliches=["was written"],
        total_sentences=len(sentences) + 1,
        flagged_count=len(sentences),
    )

    targets = _prefill_targets(report, draft)
    assert len(targets) == MAX_PREFILL_TARGETS
    assert "She smiles softly." in [span for span, _ in targets]
