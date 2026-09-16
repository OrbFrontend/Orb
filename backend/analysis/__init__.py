"""Pure prose-quality detection and editor patching helpers."""

from __future__ import annotations

from .audit import AUDIT_TYPES, AuditReport, format_report, report_to_dict, run_audit
from .detectors.anti_echo import EchoResult
from .detectors.opening_monotony import FlaggedOpener, MonotonyResult
from .detectors.phrase_repetition import PhraseResult
from .detectors.slop_detector import DetectionResult
from .detectors.structural_repetition import StructuralResult
from .detectors.template_repetition import FlaggedTemplate, TemplateResult
from .patching import (
    PatchError,
    PatchErrorKind,
    apply_id_patches,
    filter_audit_report_to_text,
)
from .speech import speech_input, speech_segments
from .targets import Target, build_targets, format_numbered_report, target_ids_for
from .text.markup import classify_axes, narration_only, protected_runs, spoken_lines
from .text.roleplay import AxisStyle, Dialogue, Narration
from .text.roleplay_segmentation import split_narration_sentences

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
    "Dialogue",
    "Narration",
    "classify_axes",
    "narration_only",
    "protected_runs",
    "spoken_lines",
    "speech_segments",
    "speech_input",
    "split_narration_sentences",
]
