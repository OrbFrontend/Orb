"""Resolve audit findings into id-addressable draft locations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..core.text_segmentation import split_paragraphs
from .audit import (
    CLEAN_REPORT,
    OUTER_MARKERS,
    AuditReport,
    negation_reason,
    strip_markers,
)
from .detectors.negated_narration import NegationFinding
from .detectors.opening_monotony import FlaggedOpener
from .detectors.template_repetition import FlaggedTemplate
from .text.roleplay_segmentation import extract_block_spans


@dataclass
class Target:
    """One addressable location in the draft."""

    tid: int  # 1-based id shown in the numbered report
    span: str  # byte-exact draft text at [start:end]
    start: int  # char offset in the draft
    end: int
    reasons: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    occurrence: int = 0  # which flagged copy of `span` this is (0-based)
    n_occurrences: int = 1  # how many flagged copies of `span` carry an id

    @property
    def is_duplicate(self) -> bool:
        return self.n_occurrences > 1


def _narration_mask(draft: str) -> list[bool]:
    """True at offsets that are narration (outside quoted speech)."""
    mask = [True] * len(draft)
    cursor = 0
    for para in split_paragraphs(draft):
        idx = draft.find(para, cursor)
        if idx < 0:
            continue
        cursor = idx + len(para)
        for typ, start, end in extract_block_spans(para):
            if typ == "SPEECH":
                for i in range(idx + start, min(idx + end, len(draft))):
                    mask[i] = False
    return mask


def _occurrences(draft: str, span: str, mask: list[bool]) -> list[int]:
    """Return occurrences of *span*, preferring narration."""
    raw: list[int] = []
    pos = draft.find(span)
    while pos >= 0:
        raw.append(pos)
        pos = draft.find(span, pos + 1)
    narration = [p for p in raw if mask[p]]
    return narration or raw


def _repeats(item: FlaggedOpener | FlaggedTemplate) -> list[str]:
    """The listed sentences that repeat an earlier member of their group.

    The first member is the original and stays, unless it lives in the earlier
    context and filtering dropped it -- then every listed sentence repeats it.
    """
    return item.sentences[1:] if item.original_listed else item.sentences


def _raw_findings(report: AuditReport, draft: str) -> list[tuple[str, str, str]]:
    """Return ``(span, category, reason)`` triples for actionable findings."""
    raw: list[tuple[str, str, str]] = []
    for fs in report.cliche_result.flagged_sentences:
        phrases = ", ".join(f'"{h.phrase}"' for h in fs.cliches)
        raw.append((fs.sentence, "banned_phrases", f"contains banned phrase(s): {phrases}"))
    for fo in report.monotony_result.flagged_openers:
        for s in _repeats(fo):
            raw.append((s, "repetitive_openers", f'opens with "{fo.opener}" like too many nearby sentences'))
    for ft in report.template_result.flagged_templates:
        for s in _repeats(ft):
            raw.append((s, "repetitive_templates", f'follows the repeated sentence structure "{ft.template}"'))
    for nb in report.not_but_result:
        if nb.get("sentence"):
            raw.append(
                (
                    nb["sentence"],
                    "contrastive_negation",
                    "uses the contrastive-negation cliché ('not X, but Y')",
                )
            )
    if report.phrase_result:
        for fp in report.phrase_result.flagged_phrases:
            for s in reversed(fp.example_sentences):
                if s in draft:
                    raw.append(
                        (
                            s,
                            "phrase_repetition",
                            f'reuses the phrase "{fp.phrase}" already seen in previous messages',
                        )
                    )
                    break
    if report.echo_result:
        for fe in report.echo_result.flagged_echoes:
            raw.append((fe.echo, "anti_echo", "parrots the user's own words back as a question"))
    return raw


class StaleSourceError(ValueError):
    """An offset-anchored finding was produced for different text than the draft."""


def check_negation_source(report: AuditReport, draft: str) -> None:
    """Reject negated-narration findings that were not produced for *draft*.

    Their offsets address the exact text the detector read; resolving them
    against any other text by substring search would patch the wrong copy.
    """
    result = report.negation_result
    if result is None:
        return
    if result.source_text != draft:
        raise StaleSourceError("negated-narration findings belong to a different draft; re-audit it")
    for finding in result.findings:
        if draft[finding.start : finding.end] != finding.span:
            raise StaleSourceError(f"negated-narration finding at {finding.start}:{finding.end} does not match its span")


def negation_interval(finding: NegationFinding, draft: str) -> tuple[int, int] | None:
    """The finding's interval with outer markers trimmed, as the patcher expects.

    Trimming happens inside the known interval, so inner emphasis stays in the
    target and no other copy of the text is ever consulted.
    """
    span = draft[finding.start : finding.end]
    lead = len(span) - len(span.lstrip().lstrip(OUTER_MARKERS).lstrip())
    trail = len(span) - len(span.rstrip().rstrip(OUTER_MARKERS).rstrip())
    start, end = finding.start + lead, finding.end - trail
    if start >= end or draft[start:end] != strip_markers(span):
        return None
    return start, end


def _negation_targets(report: AuditReport, draft: str) -> list[Target]:
    check_negation_source(report, draft)
    targets: list[Target] = []
    for finding in report.negation_findings:
        interval = negation_interval(finding, draft)
        if interval is None:
            continue
        start, end = interval
        targets.append(
            Target(
                tid=0,
                span=draft[start:end],
                start=start,
                end=end,
                reasons=[negation_reason(finding)],
                categories=["negated_narration"],
            )
        )
    return targets


def _merge_overlapping(targets: list[Target], draft: str) -> list[Target]:
    """Merge overlapping targets in document order."""
    merged: list[Target] = []
    for target in sorted(targets, key=lambda t: (t.start, -t.end)):
        prev = merged[-1] if merged else None
        if prev is not None and target.start < prev.end:
            prev.end = max(prev.end, target.end)
            prev.span = draft[prev.start : prev.end]
            for why, cat in zip(target.reasons, target.categories):
                if why not in prev.reasons:
                    prev.reasons.append(why)
                    prev.categories.append(cat)
            continue
        merged.append(target)
    return merged


def build_targets(report: AuditReport, draft: str) -> list[Target]:
    """Resolve an audit report into ordered, id-addressable targets."""
    mask = _narration_mask(draft)

    # Group findings by marker-stripped span text, preserving discovery order.
    by_span: dict[str, list[tuple[str, str]]] = {}
    for span, cat, why in _raw_findings(report, draft):
        core = strip_markers(span)
        if not core or core not in draft:
            continue
        by_span.setdefault(core, []).append((cat, why))

    targets: list[Target] = []
    for span, entries in by_span.items():
        offsets = _occurrences(draft, span, mask)
        if not offsets:
            continue
        per_cat: dict[str, int] = {}
        for cat, _ in entries:
            per_cat[cat] = per_cat.get(cat, 0) + 1
        n_locations = min(max(per_cat.values()), len(offsets))

        reasons: list[str] = []
        categories: list[str] = []
        for cat, why in entries:
            if why not in reasons:
                reasons.append(why)
                categories.append(cat)

        for k in range(n_locations):
            targets.append(
                Target(
                    tid=0,
                    span=span,
                    start=offsets[k],
                    end=offsets[k] + len(span),
                    reasons=list(reasons),
                    categories=list(categories),
                )
            )

    # Offset-anchored findings join after the text-resolved ones and never go
    # through `_occurrences`; only genuinely overlapping regions merge.
    targets.extend(_negation_targets(report, draft))
    resolved = _merge_overlapping(targets, draft)

    groups: dict[str, list[Target]] = {}
    for target in resolved:
        groups.setdefault(target.span, []).append(target)
    for group in groups.values():
        for k, target in enumerate(group):
            target.occurrence = k
            target.n_occurrences = len(group)

    for tid, target in enumerate(resolved, 1):
        target.tid = tid
    return resolved


def target_ids_for(targets: Sequence[Target], snippet: str) -> list[int]:
    """Return ids whose target region contains *snippet*."""
    core = strip_markers(snippet)
    if not core:
        return []
    return [t.tid for t in targets if core in t.span]


def negation_target_ids(targets: Sequence[Target], finding: NegationFinding, draft: str) -> list[int]:
    """Return ids whose target contains the finding's anchored interval."""
    interval = negation_interval(finding, draft)
    if interval is None:
        return []
    start, end = interval
    return [t.tid for t in targets if t.start <= start and end <= t.end]


def format_numbered_report(targets: Sequence[Target]) -> str:
    """Format numbered targets for the editor."""
    if not targets:
        return CLEAN_REPORT
    lines = ["*** WRITING AUDIT REPORT ***\n", "Numbered issues — patch each by its [id].\n"]
    for target in targets:
        lines.append(f"[{target.tid}] {strip_markers(target.span)}")
        for why in target.reasons:
            lines.append(f"      → {why}")
        if target.is_duplicate:
            lines.append(f"      → identical copy {target.occurrence + 1} of {target.n_occurrences} — each copy has its own id")
        lines.append("")
    lines.append("*** END OF REPORT ***")
    return "\n".join(lines)
