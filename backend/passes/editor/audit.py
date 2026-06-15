"""
audit.py — Run all programmatic scanners and produce a consolidated AuditReport.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .slop_detector import DetectionResult, detect_cliches

if TYPE_CHECKING:
    from ...database.models import PhraseGroup
from .contrastive_negation import detect_contrastive_negation
from .opening_monotony import MonotonyResult, detect_opening_monotony
from .phrase_repetition import PhraseResult, detect_phrase_repetition
from .structural_repetition import StructuralResult, detect_structural_repetition
from .template_repetition import TemplateResult, detect_template_repetition

# Audit toggles
#
# Each key names one programmatic scanner that the Output Auditor can run. The
# UI exposes a checkbox per type and persists the on/off state; the editor pass
# passes the resulting dict to run_audit, which skips any disabled scanner.
# Order here is the order the toggles render in the UI.
AUDIT_TYPES = (
    "banned_phrases",
    "repetitive_openers",
    "repetitive_templates",
    "contrastive_negation",
    "phrase_repetition",
    "structural_repetition",
)

DEFAULT_AUDIT_TOGGLES = {t: True for t in AUDIT_TYPES}


def _on(toggles: dict | None, key: str) -> bool:
    """Whether scanner *key* is enabled. Missing key / None toggles → enabled,
    so callers (and older databases) default to the prior all-on behaviour."""
    return True if toggles is None else bool(toggles.get(key, True))


# Data container


class AuditReport:
    __slots__ = (
        "cliche_result",
        "monotony_result",
        "template_result",
        "not_but_result",
        "phrase_result",
        "structural_repetition_result",
    )

    def __init__(
        self,
        cliche_result: DetectionResult,
        monotony_result: MonotonyResult,
        template_result: TemplateResult,
        not_but_result: list[dict] | None = None,
        phrase_result: PhraseResult | None = None,
        structural_repetition_result: StructuralResult | None = None,
    ):
        self.cliche_result = cliche_result
        self.monotony_result = monotony_result
        self.template_result = template_result
        self.not_but_result = not_but_result or []
        self.phrase_result = phrase_result
        self.structural_repetition_result = structural_repetition_result

    @classmethod
    def clean(cls) -> "AuditReport":
        """Return a clean report with zero issues (used when audit is disabled)."""
        return cls(
            cliche_result=DetectionResult([], [], 0, 0),
            monotony_result=MonotonyResult([], {}, 0, 0.0),
            template_result=TemplateResult([], {}, 0, 0, 0.0),
            not_but_result=[],
            phrase_result=None,
            structural_repetition_result=None,
        )

    @property
    def is_clean(self) -> bool:
        is_structural_clean = self.structural_repetition_result is None or not self.structural_repetition_result.is_repetitive
        is_phrase_clean = self.phrase_result is None or len(self.phrase_result.flagged_phrases) == 0
        return (
            self.cliche_result.flagged_count == 0
            and len(self.monotony_result.flagged_openers) == 0
            and len(self.template_result.flagged_templates) == 0
            and len(self.not_but_result) == 0
            and is_phrase_clean
            and is_structural_clean
        )

    @property
    def total_issues(self) -> int:
        structural_issues = 1 if self.structural_repetition_result and self.structural_repetition_result.is_repetitive else 0
        phrase_issues = len(self.phrase_result.flagged_phrases) if self.phrase_result else 0
        return (
            self.cliche_result.flagged_count
            + len(self.monotony_result.flagged_openers)
            + len(self.template_result.flagged_templates)
            + len(self.not_but_result)
            + phrase_issues
            + structural_issues
        )


# Run all scanners


def run_audit(
    text: str,
    phrase_bank: list[PhraseGroup],
    cliche_threshold: float = 0.25,
    opener_n_words: int = 1,
    opener_min_consecutive: int = 4,
    template_max_tags: int = 8,
    template_flag_threshold: int = 2,
    structural_similarity_threshold: float = 0.75,
    structural_min_complexity: int = 2,
    phrase_min_n: int = 3,
    phrase_max_n: int = 5,
    phrase_min_messages: int = 3,
    phrase_min_content_words: int = 2,
    assistant_messages: list[str] | None = None,
    structural_text: str | None = None,
    audit_toggles: dict | None = None,
) -> AuditReport:
    """Run the enabled audit scanners on the text.

    Args:
        text: The current text to audit (may be concatenated context for
            cliche/opener/template detectors).
        phrase_bank: List of banned phrase groups.
        assistant_messages: Optional list of previous assistant messages for
            structural repetition detection.
        structural_text: The current draft message for structural repetition.
            When provided, used instead of `text` so that callers that pass a
            concatenated context blob as `text` still get correct per-message
            comparison.  Defaults to `text` when omitted.
        audit_toggles: Optional per-scanner on/off map keyed by AUDIT_TYPES.
            Disabled scanners are skipped and return an empty result. None (the
            default) runs every scanner.
    """
    # Structural repetition and exact phrase repetition are cross-message checks
    # that need the draft as a standalone message plus the previous ones.
    structural_result = None
    phrase_result = None
    if assistant_messages:
        current_msg = structural_text if structural_text is not None else text
        if _on(audit_toggles, "structural_repetition"):
            structural_result = detect_structural_repetition(
                assistant_messages + [current_msg],
                similarity_threshold=structural_similarity_threshold,
                min_complexity=structural_min_complexity,
            )
        if _on(audit_toggles, "phrase_repetition"):
            # The draft must be last so require_last_message focuses flags on it.
            phrase_result = detect_phrase_repetition(
                assistant_messages + [current_msg],
                min_n=phrase_min_n,
                max_n=phrase_max_n,
                min_messages=phrase_min_messages,
                min_content_words=phrase_min_content_words,
                require_last_message=True,
            )

    return AuditReport(
        cliche_result=(
            detect_cliches(text, phrase_bank, cliche_threshold)
            if _on(audit_toggles, "banned_phrases")
            else DetectionResult([], [], 0, 0)
        ),
        monotony_result=(
            detect_opening_monotony(text, opener_n_words, opener_min_consecutive)
            if _on(audit_toggles, "repetitive_openers")
            else MonotonyResult([], {}, 0, 0.0)
        ),
        template_result=(
            detect_template_repetition(text, max_words=template_max_tags, flag_threshold=template_flag_threshold)
            if _on(audit_toggles, "repetitive_templates")
            else TemplateResult([], {}, 0, 0, 0.0)
        ),
        not_but_result=(detect_contrastive_negation(text) if _on(audit_toggles, "contrastive_negation") else []),
        phrase_result=phrase_result,
        structural_repetition_result=structural_result,
    )


# Format into text report


def _strip_asterisks(s: str) -> str:
    """Strip leading/trailing asterisks (and surrounding whitespace) from a
    snippet. Markdown emphasis markers like ``**`` confuse the rewrite model,
    which only needs the underlying text."""
    return s.strip().strip("*").strip()


def format_report(report: AuditReport) -> str:
    if report.is_clean:
        return "*** WRITING AUDIT REPORT ***\n\nAll checks passed — no issues found.\n\n*** END OF REPORT ***"

    sections: list[str] = ["*** WRITING AUDIT REPORT ***\n"]

    # 1. Banned phrases
    cr = report.cliche_result
    if cr.flagged_count > 0:
        lines = ["Banned Phrases"]
        for fs in cr.flagged_sentences:
            for hit in fs.cliches:
                lines.append(f'   - "{_strip_asterisks(hit.phrase)}" in sentence: {_strip_asterisks(fs.sentence)}')
        sections.append("\n".join(lines))

    # 2. Repetitive openers
    mr = report.monotony_result
    if mr.flagged_openers:
        lines = ["Repetitive Openers"]
        for fo in mr.flagged_openers:
            lines.append(f'   - "{_strip_asterisks(fo.opener)}" ({fo.max_run} consecutive sentences):')
            for s in fo.sentences[:4]:
                lines.append(f"     • {_strip_asterisks(s)}")
        sections.append("\n".join(lines))

    # 3. Repetitive templates
    tr = report.template_result
    if tr.flagged_templates:
        lines = ["Repetitive Templates"]
        for ft in tr.flagged_templates:
            lines.append(f'   - "{_strip_asterisks(ft.template)}" ({ft.count} sentences):')
            for s in ft.sentences[:4]:
                lines.append(f"     • {_strip_asterisks(s)}")
        sections.append("\n".join(lines))

    # 4. Not-but patterns
    if report.not_but_result:
        lines = ["Contrastive Negation Patterns (Not X, but Y)"]
        for nb in report.not_but_result:
            sentence = _strip_asterisks(nb.get("sentence", ""))
            is_parallel = nb.get("is_parallel", False)
            parallel_note = " (parallel structure)" if is_parallel else ""
            lines.append(f'   - Sentence: "{sentence}"{parallel_note}')
        sections.append("\n".join(lines))

    # 5. Exact phrase repetition (echoed across messages)
    if report.phrase_result and report.phrase_result.flagged_phrases:
        lines = ["Repeated Phrases (echoed across messages)"]
        for fp in report.phrase_result.flagged_phrases:
            lines.append(f'   - "{_strip_asterisks(fp.phrase)}" (in {fp.count} previous messages):')
            if fp.example_sentences:
                lines.append(f"     • {_strip_asterisks(fp.example_sentences[-1])}")
        sections.append("\n".join(lines))

    # 6. Structural repetition
    if report.structural_repetition_result and report.structural_repetition_result.is_repetitive:
        sr = report.structural_repetition_result
        lines = ["Structural Repetition"]
        lines.append(f"   - All {len(sr.messages)} messages share a similar block structure")
        if sr.shared_skeleton:
            skeleton_str = " → ".join(_strip_asterisks(part) for part in sr.shared_skeleton)
            lines.append(f'   - Shared skeleton: "{skeleton_str}"')
        sections.append("\n".join(lines))

    sections.append("\n*** END OF REPORT ***")
    return "\n\n".join(sections)


def _excerpt(text: str, max_len: int = 80) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
