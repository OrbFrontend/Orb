"""Pure prose-quality detection and editor patching helpers."""

from __future__ import annotations

from .audit import AUDIT_TYPES, AuditReport, format_report, report_to_dict, run_audit
from .detectors.anti_echo import EchoResult
from .detectors.opening_monotony import FlaggedOpener, MonotonyResult
from .detectors.phrase_repetition import PhraseResult
from .detectors.slop_detector import DetectionResult
from .detectors.structural_repetition import StructuralResult
from .detectors.template_repetition import FlaggedTemplate, TemplateResult
from .format_consistency import (
    AxisStyle,
    FormatDriftReport,
    baseline_axes,
    classify_axes,
    narration_only,
    normalize_to_baseline,
    protected_runs,
    spoken_lines,
    stable_label,
)
from .patching import (
    PatchError,
    PatchErrorKind,
    apply_id_patches,
    filter_audit_report_to_text,
)
from .targets import Target, build_targets, format_numbered_report, target_ids_for
from .text.text_segmentation import split_narration_sentences

__all__ = [
    "AUDIT_TYPES",
    "AuditReport",
    "format_report",
    "report_to_dict",
    "run_audit",
    "Target",
    "build_targets",
    "format_numbered_report",
    "target_ids_for",
    "apply_id_patches",
    "filter_audit_report_to_text",
    "PatchError",
    "PatchErrorKind",
    "DetectionResult",
    "MonotonyResult",
    "FlaggedOpener",
    "TemplateResult",
    "FlaggedTemplate",
    "StructuralResult",
    "PhraseResult",
    "EchoResult",
    "AxisStyle",
    "FormatDriftReport",
    "baseline_axes",
    "classify_axes",
    "narration_only",
    "normalize_to_baseline",
    "protected_runs",
    "spoken_lines",
    "stable_label",
    "split_narration_sentences",
]
