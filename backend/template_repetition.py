"""
template_repetition.py — Stub module kept for backward compatibility.

The live contrastive-negation detector has moved to contrastive_negation.py.
Template-repetition detection (POS-tag pattern matching) was removed; these
dataclasses and the no-op function remain so that audit.py and refine.py
continue to work without changes.
"""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class FlaggedTemplate:
    template: str
    count: int
    fraction: float
    sentences: List[str] = field(default_factory=list)


@dataclass
class TemplateResult:
    flagged_templates: List[FlaggedTemplate]
    all_templates: Dict[str, int]
    total_sentences: int
    unique_templates: int
    repetition_score: float


def detect_template_repetition(
    text: str,
    max_tags: int = 8,
    flag_threshold: int = 2,
    min_tags: int = 4,
) -> TemplateResult:
    return TemplateResult(
        flagged_templates=[],
        all_templates={},
        total_sentences=0,
        unique_templates=0,
        repetition_score=0.0,
    )
