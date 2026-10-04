"""Run the prose audit scanners and collect their findings."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .detectors.slop_detector import DetectionResult, detect_cliches

if TYPE_CHECKING:
    from ..database.models import PhraseGroup
from .detectors.anti_echo import EchoResult, detect_anti_echo
from .detectors.contrastive_negation import detect_contrastive_negation
from .detectors.negated_narration import NegationFinding, NegationResult, detect_negated_narration
from .detectors.opening_monotony import MonotonyResult, detect_opening_monotony
from .detectors.phrase_repetition import PhraseResult, deduplicate_phrases, detect_phrase_repetition
from .detectors.structural_repetition import StructuralResult, detect_structural_repetition
from .detectors.template_repetition import TemplateResult, detect_template_repetition

logger = logging.getLogger(__name__)

# The format normalizer is a post-editor rewrite, not an audit scanner.
AUDIT_TYPES = (
    "banned_phrases",
    "repetitive_openers",
    "repetitive_templates",
    "contrastive_negation",
    "phrase_repetition",
    "structural_repetition",
    "anti_echo",
    "negated_narration",
)

# Release defaults for scanners that do not default on. A missing key, or a
# missing map, falls back to these; every other scanner defaults on.
AUDIT_DEFAULTS: dict[str, bool] = {"negated_narration": False}


def _on(toggles: dict | None, key: str) -> bool:
    """Return whether scanner *key* is enabled."""
    default = AUDIT_DEFAULTS.get(key, True)
    return default if toggles is None else bool(toggles.get(key, default))


def _merge_phrase_results(short: PhraseResult, long: PhraseResult) -> PhraseResult:
    """Combine the short- and long-phrase repetition passes."""
    merged = deduplicate_phrases(long.flagged_phrases + short.flagged_phrases)
    return PhraseResult(flagged_phrases=merged, total_messages=short.total_messages)


class AuditReport:
    __slots__ = (
        "cliche_result",
        "monotony_result",
        "template_result",
        "not_but_result",
        "phrase_result",
        "structural_repetition_result",
        "echo_result",
        "negation_result",
    )

    def __init__(
        self,
        cliche_result: DetectionResult,
        monotony_result: MonotonyResult,
        template_result: TemplateResult,
        not_but_result: list[dict] | None = None,
        phrase_result: PhraseResult | None = None,
        structural_repetition_result: StructuralResult | None = None,
        echo_result: EchoResult | None = None,
        negation_result: NegationResult | None = None,
    ):
        self.cliche_result = cliche_result
        self.monotony_result = monotony_result
        self.template_result = template_result
        self.not_but_result = not_but_result or []
        self.phrase_result = phrase_result
        self.structural_repetition_result = structural_repetition_result
        self.echo_result = echo_result
        self.negation_result = negation_result

    @classmethod
    def clean(cls) -> AuditReport:
        """Return a clean report with zero issues (used when audit is disabled)."""
        return cls(
            cliche_result=DetectionResult([], [], 0, 0),
            monotony_result=MonotonyResult([], {}, 0, 0.0),
            template_result=TemplateResult([], {}, 0, 0, 0.0),
            not_but_result=[],
            phrase_result=None,
            structural_repetition_result=None,
            echo_result=None,
            negation_result=None,
        )

    @property
    def negation_findings(self) -> list[NegationFinding]:
        return self.negation_result.findings if self.negation_result else []

    @property
    def is_clean(self) -> bool:
        is_structural_clean = self.structural_repetition_result is None or not self.structural_repetition_result.is_repetitive
        is_phrase_clean = self.phrase_result is None or len(self.phrase_result.flagged_phrases) == 0
        is_echo_clean = self.echo_result is None or len(self.echo_result.flagged_echoes) == 0
        return (
            self.cliche_result.flagged_count == 0
            and len(self.monotony_result.flagged_openers) == 0
            and len(self.template_result.flagged_templates) == 0
            and len(self.not_but_result) == 0
            and is_phrase_clean
            and is_structural_clean
            and is_echo_clean
            and not self.negation_findings
        )

    @property
    def total_issues(self) -> int:
        structural_issues = 1 if self.structural_repetition_result and self.structural_repetition_result.is_repetitive else 0
        phrase_issues = len(self.phrase_result.flagged_phrases) if self.phrase_result else 0
        echo_issues = len(self.echo_result.flagged_echoes) if self.echo_result else 0
        return (
            self.cliche_result.flagged_count
            + len(self.monotony_result.flagged_openers)
            + len(self.template_result.flagged_templates)
            + len(self.not_but_result)
            + phrase_issues
            + structural_issues
            + echo_issues
            + len(self.negation_findings)
        )


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
    phrase_min_n: int = 2,
    phrase_max_n: int = 5,
    phrase_min_messages: int = 3,
    phrase_short_max_n: int = 2,
    phrase_long_min_messages: int = 2,
    phrase_min_content_words: int = 2,
    assistant_messages: list[str] | None = None,
    structural_text: str | None = None,
    user_message: str | None = None,
    audit_toggles: dict | None = None,
    negation_min_hits: int = 2,
) -> AuditReport:
    """Run enabled scanners on the current draft and return their findings."""
    current_msg = structural_text if structural_text is not None else text
    negation_result = None
    if _on(audit_toggles, "negated_narration"):
        # Draft only: history must not change this category's style or gate.
        negation_result = detect_negated_narration(current_msg, min_hits=negation_min_hits)
        logger.debug(
            "negated_narration: raw_hits=%d chained=%d density=%.3f",
            negation_result.raw_hits,
            len(negation_result.findings),
            negation_result.density,
        )
    echo_result = None
    if user_message and _on(audit_toggles, "anti_echo"):
        echo_result = detect_anti_echo(current_msg, user_message)
    # Structural repetition and exact phrase repetition are cross-message checks
    # that need the draft as a standalone message plus the previous ones.
    structural_result = None
    phrase_result = None
    if assistant_messages:
        if _on(audit_toggles, "structural_repetition"):
            structural_result = detect_structural_repetition(
                assistant_messages + [current_msg],
                similarity_threshold=structural_similarity_threshold,
                min_complexity=structural_min_complexity,
            )
        if _on(audit_toggles, "phrase_repetition"):
            phrase_messages = assistant_messages + [current_msg]
            short_phrases = detect_phrase_repetition(
                phrase_messages,
                min_n=phrase_min_n,
                max_n=phrase_short_max_n,
                min_messages=phrase_min_messages,
                min_content_words=phrase_min_content_words,
                require_last_message=True,
            )
            long_phrases = detect_phrase_repetition(
                phrase_messages,
                min_n=phrase_short_max_n + 1,
                max_n=phrase_max_n,
                min_messages=phrase_long_min_messages,
                min_content_words=phrase_min_content_words,
                require_last_message=True,
            )
            phrase_result = _merge_phrase_results(short_phrases, long_phrases)

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
        echo_result=echo_result,
        negation_result=negation_result,
    )


# Format into text report


# Outer markers are omitted from reported snippets so the rewrite model searches for the prose core rather than depending on a
# particular quote/emphasis style. Straight ' is excluded so contractions/possessives survive.
OUTER_MARKERS = '*_"“”‘’'


def strip_markers(s: str) -> str:
    """Strip leading/trailing emphasis (*, _) and quote markers, plus surrounding
    whitespace, from a snippet. Internal markers are left untouched."""
    return s.strip().strip(OUTER_MARKERS).strip()


_NEGATION_DESCRIPTIONS = {
    "split_contrast": "negates X only to restate it as Y",
    "stacked": "stacks negated clauses",
    "null_reaction": "narrates what doesn't happen",
    "trailing_negation": "tags a denied action onto the sentence",
}


def negation_reason(finding: NegationFinding) -> str:
    """Describe a chained negated-narration finding from its constituents."""
    parts: list[str] = []
    for part in finding.constituents:
        if part.kind == "cascade":
            parts.append(f"{part.sentence_count} consecutive denials")
        elif part.kind in _NEGATION_DESCRIPTIONS:
            parts.append(_NEGATION_DESCRIPTIONS[part.kind])
    reason = ", then ".join(parts)
    if not reason.startswith(_NEGATION_DESCRIPTIONS["null_reaction"]):
        reason = f"{_NEGATION_DESCRIPTIONS['null_reaction']}: {reason}"
    if finding.pivot_span:
        words = strip_markers(finding.pivot_span).split()
        lead = " ".join(words[:4]) + ("…" if len(words) > 4 else "")
        reason += f', before the payoff "{lead}"'
    return reason


# Both renderings — sectioned here, numbered in ``targets.format_numbered_report``
# — say the same thing when there is nothing to say.
CLEAN_REPORT = "*** WRITING AUDIT REPORT ***\n\nAll checks passed — no issues found.\n\n*** END OF REPORT ***"


def format_report(report: AuditReport) -> str:
    if report.is_clean:
        return CLEAN_REPORT

    sections: list[str] = ["*** WRITING AUDIT REPORT ***\n"]

    # 1. Banned phrases
    cr = report.cliche_result
    if cr.flagged_count > 0:
        lines = ["Banned Phrases"]
        for fs in cr.flagged_sentences:
            for hit in fs.cliches:
                lines.append(f'   - "{strip_markers(hit.phrase)}" in sentence: {strip_markers(fs.sentence)}')
        sections.append("\n".join(lines))

    # 2. Repetitive openers
    mr = report.monotony_result
    if mr.flagged_openers:
        lines = ["Repetitive Openers"]
        for fo in mr.flagged_openers:
            lines.append(f'   - "{strip_markers(fo.opener)}" ({fo.max_run} consecutive sentences):')
            for s in fo.sentences[:4]:
                lines.append(f"     • {strip_markers(s)}")
        sections.append("\n".join(lines))

    # 3. Repetitive templates
    tr = report.template_result
    if tr.flagged_templates:
        lines = ["Repetitive Templates"]
        for ft in tr.flagged_templates:
            lines.append(f'   - "{strip_markers(ft.template)}" ({ft.count} sentences):')
            for s in ft.sentences[:4]:
                lines.append(f"     • {strip_markers(s)}")
        sections.append("\n".join(lines))

    # 4. Not-but patterns
    if report.not_but_result:
        lines = ["Contrastive Negation Patterns (Not X, but Y)"]
        for nb in report.not_but_result:
            sentence = strip_markers(nb.get("sentence", ""))
            is_parallel = nb.get("is_parallel", False)
            parallel_note = " (parallel structure)" if is_parallel else ""
            lines.append(f'   - Sentence: "{sentence}"{parallel_note}')
        sections.append("\n".join(lines))

    # 5. Exact phrase repetition (echoed across messages)
    if report.phrase_result and report.phrase_result.flagged_phrases:
        lines = ["Repeated Phrases (echoed across messages)"]
        # Group phrases that cite the same target sentence so it's shown once, not
        # repeated under every phrase that happens to live in it.
        groups: dict[str, list] = {}
        for fp in report.phrase_result.flagged_phrases:
            sentence = fp.example_sentences[-1] if fp.example_sentences else ""
            groups.setdefault(sentence, []).append(fp)
        for sentence, fps in groups.items():
            for j, fp in enumerate(fps):
                suffix = ":" if sentence and j == len(fps) - 1 else ""
                lines.append(f'   - "{strip_markers(fp.phrase)}" (in {fp.count} previous messages){suffix}')
            if sentence:
                lines.append(f"     • {strip_markers(sentence)}")
        sections.append("\n".join(lines))

    # 6. Structural repetition
    if report.structural_repetition_result and report.structural_repetition_result.is_repetitive:
        sr = report.structural_repetition_result
        lines = ["Structural Repetition"]
        lines.append(f"   - All {len(sr.messages)} messages share a similar block structure")
        if sr.shared_skeleton:
            skeleton_str = " → ".join(strip_markers(part) for part in sr.shared_skeleton)
            lines.append(f'   - Shared skeleton: "{skeleton_str}"')
        sections.append("\n".join(lines))

    # 7. Anti-echo (parroting the user's last message back as a question)
    if report.echo_result and report.echo_result.flagged_echoes:
        lines = ["Interrogative Dialogue (parroting the user's dialogue back as a question)"]
        for fe in report.echo_result.flagged_echoes:
            lines.append(f'   - "{strip_markers(fe.echo)}" repeats the user\'s words: "{strip_markers(fe.matched_phrase)}"')
        sections.append("\n".join(lines))

    # 8. Negated narration (repeatedly narrating what does not happen)
    if report.negation_findings:
        lines = ["Negated Narration (narrating what does not happen)"]
        for nf in report.negation_findings:
            lines.append(f'   - "{strip_markers(nf.span)}" → {negation_reason(nf)}')
        sections.append("\n".join(lines))

    sections.append("\n*** END OF REPORT ***")
    return "\n\n".join(sections)


def report_to_dict(report: AuditReport, draft: str = "") -> dict:
    """Return the report in the API's JSON shape."""
    # Imported here rather than at module scope: targets.py reads the report
    # shape this module defines, so a top-level import would cycle.
    from .targets import build_targets, negation_target_ids, target_ids_for  # noqa: PLC0415 — import cycle

    targets = build_targets(report, draft) if draft else []

    def ids(snippet: str) -> dict[str, list[int]]:
        return {"ids": target_ids_for(targets, snippet)} if draft else {}

    sections: dict[str, list[dict]] = {}

    cr = report.cliche_result
    if cr.flagged_count > 0:
        sections["banned_phrases"] = [
            {"phrase": strip_markers(hit.phrase), "sentence": strip_markers(fs.sentence), **ids(fs.sentence)}
            for fs in cr.flagged_sentences
            for hit in fs.cliches
        ]

    mr = report.monotony_result
    if mr.flagged_openers:
        sections["repetitive_openers"] = [
            {
                "opener": strip_markers(fo.opener),
                "count": fo.max_run,
                "sentences": [strip_markers(s) for s in fo.sentences[:4]],
                # sentences[0] is the anchor the rest are compared against, so it
                # is never a target — the ids cover the flagged remainder only.
                **({"ids": [i for s in fo.sentences[1:] for i in target_ids_for(targets, s)]} if draft else {}),
            }
            for fo in mr.flagged_openers
        ]

    tr = report.template_result
    if tr.flagged_templates:
        sections["repetitive_templates"] = [
            {
                "template": strip_markers(ft.template),
                "count": ft.count,
                "sentences": [strip_markers(s) for s in ft.sentences[:4]],
                **({"ids": [i for s in ft.sentences[1:] for i in target_ids_for(targets, s)]} if draft else {}),
            }
            for ft in tr.flagged_templates
        ]

    if report.not_but_result:
        sections["contrastive_negation"] = [
            {
                "sentence": strip_markers(nb.get("sentence", "")),
                "parallel": bool(nb.get("is_parallel", False)),
                **ids(nb.get("sentence", "")),
            }
            for nb in report.not_but_result
        ]

    if report.phrase_result and report.phrase_result.flagged_phrases:
        sections["phrase_repetition"] = [
            {
                "phrase": strip_markers(fp.phrase),
                "count": fp.count,
                "sentence": strip_markers(fp.example_sentences[-1]) if fp.example_sentences else "",
                **ids(fp.example_sentences[-1] if fp.example_sentences else ""),
            }
            for fp in report.phrase_result.flagged_phrases
        ]

    sr = report.structural_repetition_result
    if sr is not None and sr.is_repetitive:
        sections["structural_repetition"] = [
            {
                "message_count": len(sr.messages),
                "skeleton": [strip_markers(part) for part in (sr.shared_skeleton or [])],
                # No span to patch — this finding is only ever fixed by a rewrite.
                **({"ids": []} if draft else {}),
            }
        ]

    if report.echo_result and report.echo_result.flagged_echoes:
        sections["anti_echo"] = [
            {"echo": strip_markers(fe.echo), "matched": strip_markers(fe.matched_phrase), **ids(fe.echo)}
            for fe in report.echo_result.flagged_echoes
        ]

    if report.negation_findings:
        sections["negated_narration"] = [
            {
                "kinds": list(nf.kinds),
                "span": strip_markers(nf.span),
                **({"ids": negation_target_ids(targets, nf, draft)} if draft else {}),
            }
            for nf in report.negation_findings
        ]

    return {"total_issues": report.total_issues, "is_clean": report.is_clean, "sections": sections}
