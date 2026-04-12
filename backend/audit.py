"""
audit.py — Run all three programmatic scanners and produce a consolidated Audit Report.
"""
from __future__ import annotations

from .slop_detector import detect_cliches, DetectionResult
from .opening_monotony import detect_opening_monotony, MonotonyResult
from .template_repetition import detect_template_repetition, TemplateResult


# Data container

class AuditReport:
    __slots__ = ("cliche_result", "monotony_result", "template_result")

    def __init__(
        self,
        cliche_result: DetectionResult,
        monotony_result: MonotonyResult,
        template_result: TemplateResult,
    ):
        self.cliche_result = cliche_result
        self.monotony_result = monotony_result
        self.template_result = template_result

    @classmethod
    def clean(cls) -> "AuditReport":
        """Return a clean report with zero issues (used when audit is disabled)."""
        return cls(
            cliche_result=DetectionResult([], [], 0, 0),
            monotony_result=MonotonyResult([], {}, 0, 0.0),
            template_result=TemplateResult([], {}, 0, 0, 0.0),
        )

    @property
    def is_clean(self) -> bool:
        return (
            self.cliche_result.flagged_count == 0
            and len(self.monotony_result.flagged_openers) == 0
            and len(self.template_result.flagged_templates) == 0
        )

    @property
    def total_issues(self) -> int:
        return (
            self.cliche_result.flagged_count
            + len(self.monotony_result.flagged_openers)
            + len(self.template_result.flagged_templates)
        )


# Run all scanners

def run_audit(
    text: str,
    phrase_bank: list[list[str]],
    cliche_threshold: float = 0.25,
    opener_n_words: int = 3,
    opener_threshold: float = 0.15,
    template_max_tags: int = 8,
    template_flag_threshold: int = 2,
) -> AuditReport:
    return AuditReport(
        cliche_result=detect_cliches(text, phrase_bank, cliche_threshold),
        monotony_result=detect_opening_monotony(text, opener_n_words, opener_threshold),
        template_result=detect_template_repetition(text, template_max_tags, template_flag_threshold),
    )


# Format into text report

def format_report(report: AuditReport) -> str:
    if report.is_clean:
        return "*** REFINEMENT AUDIT REPORT ***\n\nAll checks passed — no issues found.\n\n*** END OF REPORT ***"

    sections: list[str] = ["*** REFINEMENT AUDIT REPORT ***\n"]

    # 1. Banned phrases
    cr = report.cliche_result
    if cr.flagged_count > 0:
        lines = [
            "1. Banned Phrases — Completely rewrite and replace each flagged sentence to eliminate the "
            "banned phrases entirely. Make a creative and bold effort, do not just substitute with similar, related words."
        ]
        for fs in cr.flagged_sentences:
            for hit in fs.cliches:
                if hit.canonical != hit.variant:
                    lines.append(f'   - "{hit.canonical}" (variant "{hit.variant}") in sentence: {fs.sentence}')
                else:
                    lines.append(f'   - "{hit.canonical}" in sentence: {fs.sentence}')
        sections.append("\n".join(lines))
    else:
        sections.append("1. Banned Phrases — CLEAN")

    # 2. Repetitive openers
    mr = report.monotony_result
    if mr.flagged_openers:
        lines = [
            "2. Repetitive Openers — Rewrite and replace flagged sentences so they no longer "
            "begin with the same opening words. Vary the sentence structure."
        ]
        for fo in mr.flagged_openers:
            lines.append(f'   - "{fo.opener}" (appeared {fo.count} times):')
            for s in fo.sentences[:4]:
                lines.append(f"     • {s}")
        sections.append("\n".join(lines))
    else:
        sections.append("2. Repetitive Openers — CLEAN")

    # 3. Repetitive templates
    tr = report.template_result
    if tr.flagged_templates:
        lines = [
            "3. Repetitive Templates — Restructure flagged sentences so they no "
            "longer follow the same POS pattern. Change clause order, combine sentences, or vary syntax."
        ]
        for ft in tr.flagged_templates:
            lines.append(f'   - "{ft.template}" ({ft.count} sentences):')
            for s in ft.sentences[:4]:
                lines.append(f"     • {s}")
        sections.append("\n".join(lines))
    else:
        sections.append("3. Repetitive Templates — CLEAN")

    sections.append("\n*** END OF REPORT ***")
    return "\n\n".join(sections)


def _excerpt(text: str, max_len: int = 80) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."