"""Analysis layer — pure prose-quality detection.

Depends only on ``database.models`` (+ stdlib); it does **not** depend on
``core`` or ``inference``. It sits below ``workflows`` and ``pipeline``,
parallel to ``inference`` — shared by the editor pass (``pipeline.passes.editor``)
and the workflow tools (``workflows.toolkit``). Extracting it is what keeps the
one-way rule (it was the lone ``workflows → passes`` back-edge).

The facade re-exports the auditor entry points and the public result types.
Detector *functions* (``detect_cliches``, ``detect_opening_monotony``, …) and
private helpers are reached via ``analysis.detectors.<module>`` directly.
"""

from __future__ import annotations

from .audit import AUDIT_TYPES, AuditReport, format_report, run_audit
from .detectors.opening_monotony import FlaggedOpener, MonotonyResult
from .detectors.phrase_repetition import PhraseResult
from .detectors.slop_detector import DetectionResult
from .detectors.structural_repetition import StructuralResult
from .detectors.template_repetition import FlaggedTemplate, TemplateResult
from .detectors.text_segmentation import split_narration_sentences

__all__ = [
    # audit — consolidated runner + report
    "AUDIT_TYPES",
    "AuditReport",
    "format_report",
    "run_audit",
    # detector result types
    "DetectionResult",
    "MonotonyResult",
    "FlaggedOpener",
    "TemplateResult",
    "FlaggedTemplate",
    "StructuralResult",
    "PhraseResult",
    # shared segmentation helper
    "split_narration_sentences",
]
