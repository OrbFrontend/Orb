from backend.analysis import build_targets
from backend.analysis.audit import AuditReport
from backend.analysis.detectors.opening_monotony import FlaggedOpener, MonotonyResult
from backend.analysis.detectors.slop_detector import DetectionResult
from backend.analysis.detectors.template_repetition import (
    FlaggedTemplate,
    TemplateResult,
)
from backend.analysis.patching import filter_audit_report_to_text


def test_filter_audit_report_supports_slotted_detector_items():
    sentences = ["She walks.", "She smiles."]
    report = AuditReport(
        cliche_result=DetectionResult([], [], 2, 0),
        monotony_result=MonotonyResult(
            [FlaggedOpener("she", 2, 2, 1.0, sentences)],
            {"she": 2},
            2,
            1.0,
        ),
        template_result=TemplateResult(
            [FlaggedTemplate("she", 2, 1.0, sentences)],
            {"she": 2},
            2,
            1,
            1.0,
        ),
    )

    filtered = filter_audit_report_to_text(report, "She smiles.")

    assert not hasattr(report.monotony_result.flagged_openers[0], "__dict__")
    assert filtered.monotony_result.flagged_openers == [FlaggedOpener("she", 1, 2, 0.5, ["She smiles."], original_listed=False)]
    assert filtered.template_result.flagged_templates == [
        FlaggedTemplate("she", 1, 0.5, ["She smiles."], original_listed=False)
    ]


def _templates(*sentences: str) -> AuditReport:
    report = AuditReport.clean()
    report.template_result = TemplateResult(
        [FlaggedTemplate("she looked at", len(sentences), 1.0, list(sentences))], {}, len(sentences), 1, 1.0
    )
    return report


def test_a_group_led_from_the_context_targets_every_draft_member():
    # The original sits in the previous message, so every draft sentence repeats
    # it. Sparing the first draft member as "the original" left a finding counted
    # with no id to patch, which forced a whole-draft rewrite.
    draft = "She looked at her empty hands. Then she looked at Kai."
    report = filter_audit_report_to_text(
        _templates("She looked at her.", "She looked at her empty hands.", "Then she looked at Kai."), draft
    )
    assert [t.span for t in build_targets(report, draft)] == ["She looked at her empty hands.", "Then she looked at Kai."]


def test_a_group_led_from_the_draft_still_spares_its_original():
    draft = "She looked at her empty hands. Then she looked at Kai."
    report = filter_audit_report_to_text(_templates("She looked at her empty hands.", "Then she looked at Kai."), draft)
    assert [t.span for t in build_targets(report, draft)] == ["Then she looked at Kai."]


def test_a_group_whose_repeats_all_sit_in_the_context_is_dropped():
    # The run lives in the previous message; the draft only shares the wording
    # of its first member. Counting it gave the draft an issue with nothing in
    # it to fix.
    draft = '"Fine," she said. He left.'
    report = filter_audit_report_to_text(_templates("she said.", "She held his look.", "She tilted her head."), draft)
    assert report.template_result.flagged_templates == []
    assert report.is_clean
