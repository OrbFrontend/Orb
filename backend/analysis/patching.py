"""
patching.py — Pure audit-report filtering and search/replace patch application.

Extracted from ``pipeline.passes.editor.editor`` so non-pipeline consumers
(the Document-mode auditor in ``features/documents``) can reuse them without
importing a peer slice: pipeline→analysis and features→analysis are both legal
downward edges. The editor re-imports these names, so its public surface is
unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import fields

from .audit import AuditReport
from .detectors.opening_monotony import FlaggedOpener, MonotonyResult
from .detectors.slop_detector import DetectionResult
from .detectors.template_repetition import FlaggedTemplate, TemplateResult
from .text.text_segmentation import split_narration_sentences

logger = logging.getLogger(__name__)


# ── Audit-report filtering ────────────────────────────────────────────────────


def _split_target_sentences(target_text: str) -> set[str]:
    """Split *target_text* into a sentence set using the same heuristic as the detectors."""
    return set(split_narration_sentences(target_text))


def _filter_flagged_items(items, sentences: set[str], total: int, *, cls, label_field: str):
    """Filter a list of FlaggedOpener or FlaggedTemplate to sentences in *sentences*.

    Returns a list of *cls* instances with adjusted count and fraction.
    """
    filtered = []
    for item in items:
        kept = [s for s in item.sentences if s in sentences]
        if kept:
            extra = {
                descriptor.name: getattr(item, descriptor.name)
                for descriptor in fields(item)
                if descriptor.name not in (label_field, "count", "fraction", "sentences")
            }
            filtered.append(
                cls(
                    **{label_field: getattr(item, label_field)},
                    count=len(kept),
                    fraction=len(kept) / total if total > 0 else 0.0,
                    sentences=kept,
                    **extra,
                )
            )
    return filtered


def filter_audit_report_to_text(report: AuditReport, target_text: str) -> AuditReport:
    """Narrow an audit report to only flag sentences that appear in *target_text*.

    Used when the audit ran on concatenated text (draft + previous messages) but
    only issues in the draft itself should be surfaced.
    """
    target_sents = _split_target_sentences(target_text)

    # Cliché results — slop_detector splits only on [.!?] while _split_target_sentences
    # also splits after quote chars, so sentences may not match as set members.
    # Use substring containment instead, which is guaranteed correct since the
    # detector can only flag sentences it found within the text.
    filtered_fs = [fs for fs in report.cliche_result.flagged_sentences if fs.sentence in target_text]
    filtered_cliche = DetectionResult(
        flagged_sentences=filtered_fs,
        unique_cliches=report.cliche_result.unique_cliches,
        total_sentences=report.cliche_result.total_sentences,
        flagged_count=len(filtered_fs),
    )

    # Opener results
    filtered_openers = _filter_flagged_items(
        report.monotony_result.flagged_openers,
        target_sents,
        report.monotony_result.total_sentences,
        cls=FlaggedOpener,
        label_field="opener",
    )
    filtered_monotony = MonotonyResult(
        flagged_openers=filtered_openers,
        all_openers=report.monotony_result.all_openers,
        total_sentences=report.monotony_result.total_sentences,
        monotony_score=report.monotony_result.monotony_score,
    )

    # Template results
    filtered_templates = _filter_flagged_items(
        report.template_result.flagged_templates,
        target_sents,
        report.template_result.total_sentences,
        cls=FlaggedTemplate,
        label_field="template",
    )
    filtered_template = TemplateResult(
        flagged_templates=filtered_templates,
        all_templates=report.template_result.all_templates,
        total_sentences=report.template_result.total_sentences,
        unique_templates=report.template_result.unique_templates,
        repetition_score=report.template_result.repetition_score,
    )

    # Not-but results — same mismatch issue as clichés (contrastive_negation also
    # splits only on [.!?]), so use substring containment here too.
    filtered_not_but = [nb for nb in report.not_but_result if nb.get("sentence", "") in target_text]

    # Structural repetition and exact phrase repetition are cross-message checks,
    # so they're always relevant when comparing the draft to previous messages.
    # Phrase repetition already focuses on the draft via require_last_message, so
    # we keep both unfiltered. Anti-echo is likewise inherently draft-scoped (it
    # only flags questions found in the draft), so it passes through unfiltered.

    return AuditReport(
        cliche_result=filtered_cliche,
        monotony_result=filtered_monotony,
        template_result=filtered_template,
        not_but_result=filtered_not_but,
        phrase_result=report.phrase_result,
        structural_repetition_result=report.structural_repetition_result,
        echo_result=report.echo_result,
    )


# ── Quote normalisation & patching ────────────────────────────────────────────

_QUOTE_MAP = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "–": "-",
        "—": "-",
    }
)

# Non-standard escape sequences some LLMs emit literally in their JSON output.
# Standard JSON escapes (\n, \t, etc.) are already decoded by json.loads;
# these are the ones that slip through as literal two-character sequences.
_LLM_ESCAPE_MAP = [
    ("\\'", "'"),
    ('\\"', '"'),
]


def _unescape_llm_artifacts(text: str) -> str:
    for esc, ch in _LLM_ESCAPE_MAP:
        text = text.replace(esc, ch)
    return text


def _normalize_quotes(text: str) -> str:
    return text.translate(_QUOTE_MAP)


# Boundary markers the audit report deliberately omits from snippets. A search
# copied from the report can therefore differ from the marked-up draft. Straight
# ' is excluded so contractions/possessives survive.
_OUTER_MARKERS = '*_"“”‘’'


def _strip_outer_markers(text: str) -> str:
    """Strip leading/trailing emphasis (*, _) and quote markers, plus the
    whitespace just inside them.  Internal markers are left untouched."""
    return text.strip().strip(_OUTER_MARKERS).strip()


def apply_patches(draft: str, patches: list[dict]) -> tuple[str, list[str]]:
    """Apply search/replace patches to *draft*. Returns ``(updated_draft, error_messages)``."""
    errors: list[str] = []
    logger.debug("Applying %d patches to draft (%d chars)", len(patches), len(draft))

    for i, p in enumerate(patches):
        # A malformed tool call can place a non-dict where the schema expects a
        # {search, replace} object; skip it rather than crash on .get().
        if not isinstance(p, dict):
            logger.debug("Patch %d: non-dict element (%s), skipping", i, type(p).__name__)
            continue
        search = _unescape_llm_artifacts(p.get("search", ""))
        replace = _unescape_llm_artifacts(p.get("replace", ""))
        if not search:
            logger.debug("Patch %d: empty search string, skipping", i)
            continue
        if search == replace:
            err = f"Error: Patch {i} is a no-op (search === replace). You must provide different replacement text."
            errors.append(err)
            continue

        # The audit report strips outer boundary markers, while a model may add
        # them back inconsistently. Match on the marker-stripped core and replace
        # the core, leaving the draft's own surrounding markers in place — this
        # keeps quotes/emphasis balanced whether the draft has an extra trailing
        # marker (…octave.*) or the search a spurious leading quote ("Do not…).
        # Falls through when the core is ambiguous (markers were load-bearing for
        # uniqueness) so the exact strategies below can target the marked span.
        core_search = _strip_outer_markers(search)
        core_replace = _strip_outer_markers(replace)
        if core_search and core_search != core_replace and (core_search != search or core_replace != replace):
            if draft.count(core_search) == 1:
                draft = draft.replace(core_search, core_replace, 1)
                logger.debug("Patch %d OK (marker-core): %r → %r", i, core_search[:60], core_replace[:60])
                continue

        count = draft.count(search)

        # Fallback: try with normalised quotes when exact match fails
        if count == 0:
            norm_search = _normalize_quotes(search)
            norm_draft = _normalize_quotes(draft)
            norm_count = norm_draft.count(norm_search)
            if norm_count == 1:
                pos = norm_draft.index(norm_search)
                original_substr = draft[pos : pos + len(norm_search)]
                if len(original_substr) == len(norm_search):
                    draft = draft[:pos] + replace + draft[pos + len(original_substr) :]
                    logger.debug(
                        "Patch %d OK (quote-normalized): %r → %r",
                        i,
                        search[:60],
                        replace[:60],
                    )
                    continue
            elif norm_count > 1:
                errors.append(
                    f"Error: Multiple matches ({norm_count}) for {search[:80]!r} (after quote normalization). Use more context."
                )
                continue

            errors.append(f"Error: {search[:80]!r} not found in draft.")

        elif count > 1:
            errors.append(f"Error: Multiple matches ({count}) for {search[:80]!r}. Use more context.")
        else:
            draft = draft.replace(search, replace, 1)
            logger.debug("Patch %d OK: %r → %r", i, search[:60], replace[:60])

    logger.debug("Patch application done: %d errors out of %d patches", len(errors), len(patches))
    return draft, errors
