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

    filtered = filter_audit_report_to_text(report, "She walks.")

    assert not hasattr(report.monotony_result.flagged_openers[0], "__dict__")
    assert filtered.monotony_result.flagged_openers == [FlaggedOpener("she", 1, 2, 0.5, ["She walks."])]
    assert filtered.template_result.flagged_templates == [FlaggedTemplate("she", 1, 0.5, ["She walks."])]
