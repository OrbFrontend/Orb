"""targets.py — Audit findings resolved into ID-addressable draft locations.

The editor used to anchor each edit on a literal ``search`` string the model
copied out of the draft. The audit already knows every flagged sentence
byte-exactly, so this module numbers the findings instead: the report labels
each one ``[N]``, the model answers ``{"id": N, "replace": "…"}``, and
:func:`analysis.patching.apply_id_patches` splices the recorded offsets. No
search, no fallback ladder, and a sentence that occurs twice stays addressable
instead of being dropped as ambiguous.

A **target** is one addressable location. It is not one finding: several
detectors can land on the same sentence (a banned phrase that is also a
repeated opener), and that is one edit, so their reasons merge.

Two segmentation facts shape the rest of the module:

* Most detectors match narration only, so occurrence resolution runs against a
  narration mask — a verbatim copy sitting inside quoted speech must not
  consume an occurrence slot, or the k-th-occurrence mapping drifts from what
  the detector actually flagged.
* The detectors do not agree on where a sentence ends. ``contrastive_negation``
  keeps dialogue inline while openers and templates strip it, so one sentence
  can yield two regions where one contains the other. Those **must** merge:
  splicing back-to-front would apply the inner patch and then overwrite it with
  the outer one, whose ``end`` offset is now stale — silent data loss, where
  the old search path would at least have reported "not found".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .audit import CLEAN_REPORT, AuditReport, _strip_markers
from .text.text_segmentation import extract_block_spans, split_paragraphs


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


# ── Occurrence resolution ─────────────────────────────────────────────────────


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
    """Offsets of every occurrence of *span*, narration preferred.

    Anti-echo is the exception to the narration rule — it flags the dialogue
    itself — so when the narration filter leaves nothing, fall back to the raw
    hits rather than losing the finding.
    """
    raw: list[int] = []
    pos = draft.find(span)
    while pos >= 0:
        raw.append(pos)
        pos = draft.find(span, pos + 1)
    narration = [p for p in raw if mask[p]]
    return narration or raw


# ── Finding enumeration ───────────────────────────────────────────────────────


def _raw_findings(report: AuditReport, draft: str) -> list[tuple[str, str, str]]:
    """``(span, category, reason)`` triples, one per finding the model can fix.

    Repeated openers/templates keep their first sentence as the anchor and flag
    the rest. Structural repetition has no span — the rewrite path owns it.
    """
    raw: list[tuple[str, str, str]] = []
    for fs in report.cliche_result.flagged_sentences:
        phrases = ", ".join(f'"{h.phrase}"' for h in fs.cliches)
        raw.append((fs.sentence, "banned_phrases", f"contains banned phrase(s): {phrases}"))
    for fo in report.monotony_result.flagged_openers:
        for s in fo.sentences[1:]:
            raw.append((s, "repetitive_openers", f'opens with "{fo.opener}" like too many nearby sentences — vary the opening'))
    for ft in report.template_result.flagged_templates:
        for s in ft.sentences[1:]:
            raw.append(
                (s, "repetitive_templates", f'follows the repeated sentence template "{ft.template}" — vary the structure')
            )
    for nb in report.not_but_result:
        if nb.get("sentence"):
            raw.append(
                (
                    nb["sentence"],
                    "contrastive_negation",
                    "uses the contrastive-negation cliché ('not X, but Y') — rephrase without it",
                )
            )
    if report.phrase_result:
        for fp in report.phrase_result.flagged_phrases:
            # example_sentences is ordered by message index and the draft is the
            # last message, so the draft's own copy sits at the end. Scan from
            # there: both report renderings show `example_sentences[-1]`
            # (audit.format_report, audit.report_to_dict), so a target resolved
            # from an earlier message's sentence — which can happen when that
            # sentence is also a substring of the draft — would leave the panel
            # showing a finding whose id it cannot name.
            for s in reversed(fp.example_sentences):
                if s in draft:
                    raw.append(
                        (
                            s,
                            "phrase_repetition",
                            f'reuses the phrase "{fp.phrase}" already seen in {fp.count} previous messages',
                        )
                    )
                    break
    if report.echo_result:
        for fe in report.echo_result.flagged_echoes:
            raw.append((fe.echo, "anti_echo", "parrots the user's own words back as a question — replace with something new"))
    return raw


def _merge_overlapping(targets: list[Target], draft: str) -> list[Target]:
    """Fold targets whose draft regions overlap into one, document-ordered.

    Two ids naming overlapping text is both a correctness hazard (see the module
    docstring) and a genuine ambiguity for the model, so the union region becomes
    a single id carrying every reason. Regions that merely abut do not merge:
    disjoint splices stay independent.
    """
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
    """Resolve an audit report into id-addressable, document-ordered targets.

    Findings whose span the detectors segmented differently from the draft (so
    it cannot be located) are dropped — a report can therefore be non-clean and
    still produce no targets, which is the caller's cue to rewrite rather than
    patch. Structural repetition always lands there: it has no span at all.
    """
    mask = _narration_mask(draft)

    # Group findings by marker-stripped span text, preserving discovery order.
    by_span: dict[str, list[tuple[str, str]]] = {}
    for span, cat, why in _raw_findings(report, draft):
        core = _strip_markers(span)
        if not core or core not in draft:
            continue
        by_span.setdefault(core, []).append((cat, why))

    targets: list[Target] = []
    for span, entries in by_span.items():
        offsets = _occurrences(draft, span, mask)
        if not offsets:
            continue
        # Distinct detector categories flagging this span describe the *same*
        # location. Repeats of one category (the cliché detector emits one
        # FlaggedSentence per occurrence) are what indicate several locations.
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

    resolved = _merge_overlapping(targets, draft)

    # Duplicate labelling is derived from the ids that actually exist, not from
    # how often the text occurs: a span the detector reported once but that
    # appears twice gets one id, and the report must not promise a second.
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
    """Ids whose region covers *snippet* — the bridge from a sectioned report
    entry back to the ids the model was given for it.

    A merged target covers more than the snippet that produced it, so
    containment (not equality) is the test; a duplicated span answers with
    every id that names a copy.
    """
    core = _strip_markers(snippet)
    if not core:
        return []
    return [t.tid for t in targets if core in t.span]


# ── Report rendering ──────────────────────────────────────────────────────────


def format_numbered_report(targets: Sequence[Target]) -> str:
    """The id-anchored twin of :func:`audit.format_report`.

    A flat, document-ordered list rather than per-category sections: the model
    patches locations, not categories, and a sentence flagged by three scanners
    is one entry with three reasons.

    An empty list renders as the clean report, which is only true when the audit
    genuinely found nothing. Callers holding a non-clean report with no targets
    must render :func:`audit.format_report` and take the rewrite path instead —
    see ``pipeline.passes.editor._build_editor_request``.
    """
    if not targets:
        return CLEAN_REPORT
    lines = ["*** WRITING AUDIT REPORT ***\n", "Numbered findings — patch each by its [id].\n"]
    for target in targets:
        lines.append(f"[{target.tid}] {_strip_markers(target.span)}")
        for why in target.reasons:
            lines.append(f"      → {why}")
        if target.is_duplicate:
            lines.append(f"      → identical copy {target.occurrence + 1} of {target.n_occurrences} — each copy has its own id")
        lines.append("")
    lines.append("*** END OF REPORT ***")
    return "\n".join(lines)
