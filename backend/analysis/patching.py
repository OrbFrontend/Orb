"""
patching.py — Pure audit-report filtering and id-anchored patch application.

Extracted from ``pipeline.passes.editor.editor`` so non-pipeline consumers
(the Document-mode auditor in ``features/documents``) can reuse them without
importing a peer slice: pipeline→analysis and features→analysis are both legal
downward edges.

Patches are anchored on the numbered findings :mod:`analysis.targets` builds,
not on a ``search`` string copied out of the draft. Application is a splice of
offsets the audit itself recorded, so the whole near-miss fallback ladder that
the string path needed (marker stripping, quote normalisation, LLM escape
artifacts) is gone along with the "Multiple matches" rejection that made a
duplicated sentence unfixable.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import fields
from typing import Any

from .audit import AuditReport
from .detectors.opening_monotony import FlaggedOpener, MonotonyResult
from .detectors.slop_detector import DetectionResult
from .detectors.template_repetition import FlaggedTemplate, TemplateResult
from .guarding import guard_protected_sequences, protected_bands
from .healing import heal_replacement
from .targets import Target
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


# ── ID-anchored patch application ─────────────────────────────────────────────


class PatchErrorKind:
    """Why a patch did not apply, for callers that must tell the cases apart.

    The text of an error is written for the model; the kind is written for the
    loop around it. An unknown id is the model naming a finding that does not
    exist and costs nothing to ignore, while ``PROTECTED_SEQUENCE`` means a real
    target was left unrepaired for a reason the model can act on — policies that
    treat those alike either chase junk ids or swallow a rejection silently.
    """

    MALFORMED = "malformed"  # not an {id, replace} object, or `replace` is not text
    UNKNOWN_ID = "unknown_id"  # names no finding in the report
    DUPLICATE_ID = "duplicate_id"  # a second patch for a finding already patched
    NO_OP = "no_op"  # `replace` repeats the flagged text, before or after healing
    RESTATED_CONTEXT = "restated_context"  # healed away entirely: all copied context
    PROTECTED_SEQUENCE = "protected_sequence"  # clones text outside the target span


class PatchError(str):
    """One error message, plus the finding and the reason behind it.

    A ``str`` subclass rather than a record, because the message *is* the
    payload for its first audience: it is fed back to the model verbatim as the
    tool result, joined, logged, and counted as a plain string by every existing
    caller. The two attributes ride along for the second audience — the editor
    loop, which needs to know that *this* rejection was a real target it can ask
    the model to redo. ``tid`` is ``None`` when the patch never named a valid
    finding at all.
    """

    tid: int | None
    kind: str

    def __new__(cls, text: str, *, tid: int | None, kind: str) -> PatchError:
        error = super().__new__(cls, text)
        error.tid = tid
        error.kind = kind
        return error


def _coerce_id(raw: object) -> int | None:
    """A finding id from whatever the model's JSON put in the field.

    The schema says integer and the forced-decoding grammar holds it to that,
    but the chat-transport recovery chain can hand back a `"2"` or a `2.0`;
    those name a finding just as clearly. A bool is an ``int`` in Python and is
    never an id, so it is rejected rather than read as 0/1.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _neighbour_bounds(draft: str, targets: Sequence[Target]) -> dict[int, tuple[int, int]]:
    """Per target id, the end of the target before it and the start of the one after.

    Document order over *all* targets, not just the patched ones: every target
    is mutable text, so a finding nobody patched still never counts as protected
    context for its neighbour. The two offsets bound the protected gaps touching
    each target — see :func:`analysis.guarding.protected_bands`, which will only
    accept gaps adjacent to their target. ``build_targets`` merges regions that
    overlap, so the clamps below are belt-and-braces for a hand-built list.
    """
    ordered = sorted(targets, key=lambda t: t.start)
    bounds: dict[int, tuple[int, int]] = {}
    for k, target in enumerate(ordered):
        previous_end = min(ordered[k - 1].end, target.start) if k else 0
        next_start = max(ordered[k + 1].start, target.end) if k + 1 < len(ordered) else len(draft)
        bounds[target.tid] = (max(previous_end, 0), min(next_start, len(draft)))
    return bounds


def _no_op_error(tid: int) -> PatchError:
    """The one no-op message, raised both on the raw `replace` and on the healed one.

    A replacement can arrive equal to the flagged text, or become equal to it
    once the context it copied is trimmed off (``Bad. C.`` over ``Bad.``). Both
    edit nothing, so both must read the same to the model and cost the same one
    error to the ``len(patches) - len(errors)`` count.
    """
    return PatchError(
        f"Error: the patch for id {tid} is a no-op — `replace` repeats the flagged text unchanged.",
        tid=tid,
        kind=PatchErrorKind.NO_OP,
    )


def apply_id_patches(draft: str, targets: Sequence[Target], patches: Sequence[Any]) -> tuple[str, list[PatchError]]:
    """Apply ``{"id": N, "replace": "…"}`` patches by splicing recorded offsets.

    *targets* must be the list that produced the report the model answered —
    ids are renumbered on every re-audit, so a stale list silently edits the
    wrong sentences.

    Every replacement is run through :func:`analysis.healing.heal_replacement`
    before it lands, which drops sentences the model copied out of the draft on
    either side of its target — the mis-aim that otherwise prints a sentence
    twice. A replacement that is *only* such copies heals to nothing and is
    rejected rather than deleting the flagged span on a guess. What healing
    leaves is then checked by
    :func:`analysis.guarding.guard_protected_sequences`, which rejects a
    replacement that clones a significant run out of the untouchable text
    beside its target — the same duplication, sitting where no trim could
    remove it safely. A rejection is per-patch: the other replacements still
    apply and the rejected target keeps the writer's original text.

    Errors are written in the report's own id vocabulary because they are fed
    back to the model verbatim as the tool result. The failure modes are a
    malformed patch, a no-op replacement (before *or* after healing), one that
    heals away entirely, and one that copies protected text; nothing here can
    fail to *find* its anchor. Exactly one error is emitted per patch that does
    not apply, so callers can count applications as
    ``len(patches) - len(errors)``. Returns ``(updated_draft, error_messages)``;
    each message is a :class:`PatchError`, which reads as the plain string the
    model is shown and carries the finding id and :class:`PatchErrorKind` for
    callers that must act differently on different failures.
    """
    errors: list[PatchError] = []
    by_id = {t.tid: t for t in targets}
    id_range = f"1-{len(targets)}" if targets else "(none — the report has no numbered findings)"
    resolved: list[tuple[Target, str]] = []
    seen_ids: set[int] = set()
    logger.debug("Applying %d id-patches to draft (%d chars, %d targets)", len(patches), len(draft), len(targets))

    for i, p in enumerate(patches):
        # A malformed tool call can place a non-dict where the schema expects an
        # {id, replace} object; report it rather than crash on .get().
        if not isinstance(p, dict):
            errors.append(
                PatchError(
                    f"Error: patch {i} is not an object with `id` and `replace`.",
                    tid=None,
                    kind=PatchErrorKind.MALFORMED,
                )
            )
            continue
        raw_id = p.get("id")
        pid = _coerce_id(raw_id)
        if pid is None:
            errors.append(
                PatchError(
                    f"Error: patch {i} has a non-integer id ({raw_id!r}). Valid ids: {id_range}.",
                    tid=None,
                    kind=PatchErrorKind.MALFORMED,
                )
            )
            continue
        target = by_id.get(pid)
        if target is None:
            errors.append(
                PatchError(
                    f"Error: no finding with id {pid} in the report. Valid ids: {id_range}.",
                    tid=pid,
                    kind=PatchErrorKind.UNKNOWN_ID,
                )
            )
            continue
        if pid in seen_ids:
            errors.append(
                PatchError(
                    f"Error: id {pid} was patched more than once. Emit exactly one patch per finding.",
                    tid=pid,
                    kind=PatchErrorKind.DUPLICATE_ID,
                )
            )
            continue
        seen_ids.add(pid)
        replace = p.get("replace")
        if replace is None:
            errors.append(
                PatchError(
                    f"Error: the patch for id {pid} has no `replace` text.",
                    tid=pid,
                    kind=PatchErrorKind.MALFORMED,
                )
            )
            continue
        # An id can be recovered from a stringy or float JSON value because every
        # such form names one finding unambiguously. Replacement prose cannot:
        # coercing a number or a nested object with str() would splice `['a', 'b']`
        # into the draft, the one input class here that would neither error nor
        # produce a correct edit.
        if not isinstance(replace, str):
            errors.append(
                PatchError(
                    f"Error: the patch for id {pid} has a non-text `replace` ({type(replace).__name__}). Send a string.",
                    tid=pid,
                    kind=PatchErrorKind.MALFORMED,
                )
            )
            continue
        if replace == target.span:
            errors.append(_no_op_error(pid))
            continue
        resolved.append((target, replace))

    # Splice back-to-front so the offsets ahead of each edit stay valid. Targets
    # never overlap (build_targets merges regions that do), so this is exact.
    # Healing reads `out`, not `draft`: back-to-front means everything after the
    # span is already in its final form, which is exactly the text a replacement
    # must not duplicate.
    out = draft
    heal_errors: list[PatchError] = []
    # Bands are sliced from the original *draft*: back-to-front application
    # leaves everything before the next target's start untouched, so the two
    # gaps adjacent to the target being patched still read as they did here.
    bounds = _neighbour_bounds(draft, targets)
    for target, replace in sorted(resolved, key=lambda r: r[0].start, reverse=True):
        healed = heal_replacement(out, target.start, target.end, replace)
        for note in healed.notes:
            logger.info("Patch id %d healed: %s", target.tid, note)
        if healed.rejection is not None:
            heal_errors.append(
                PatchError(
                    f"Error: the patch for id {target.tid} {healed.rejection}.",
                    tid=target.tid,
                    kind=PatchErrorKind.RESTATED_CONTEXT,
                )
            )
            continue
        if healed.replace == out[healed.start : healed.end]:
            heal_errors.append(_no_op_error(target.tid))
            continue
        # Guarding reads the *healed* text, so an end-aligned copy that healing
        # already trimmed is not charged twice, and an interior one left behind
        # is still caught.
        previous_end, next_start = bounds[target.tid]
        clone = guard_protected_sequences(
            healed.replace,
            protected_bands(draft, previous_end, target.start, target.end, next_start),
            target.span,
        )
        if clone is not None:
            logger.warning("Protected-sequence guard rejected patch id %d: %s", target.tid, clone.rejection)
            heal_errors.append(
                PatchError(
                    f"Error: the patch for id {target.tid} {clone.rejection}.",
                    tid=target.tid,
                    kind=PatchErrorKind.PROTECTED_SEQUENCE,
                )
            )
            continue
        out = out[: healed.start] + healed.replace + out[healed.end :]
        logger.debug("Patch id %d OK: %r → %r", target.tid, target.span[:60], healed.replace[:60])

    # Reversed so the heal rejections read in document order like the report did.
    errors.extend(reversed(heal_errors))
    logger.debug("Patch application done: %d errors out of %d patches", len(errors), len(patches))
    return out, errors
