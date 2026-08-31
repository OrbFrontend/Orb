"""Filter audit reports and apply id-anchored editor patches."""

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


def _split_target_sentences(target_text: str) -> set[str]:
    """Split *target_text* into a sentence set using the same heuristic as the detectors."""
    return set(split_narration_sentences(target_text))


def _filter_flagged_items(items, sentences: set[str], total: int, *, cls, label_field: str):
    """Filter flagged items to the supplied sentences."""
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
    """Limit an audit report to findings present in *target_text*."""
    target_sents = _split_target_sentences(target_text)

    filtered_fs = [fs for fs in report.cliche_result.flagged_sentences if fs.sentence in target_text]
    filtered_cliche = DetectionResult(
        flagged_sentences=filtered_fs,
        unique_cliches=report.cliche_result.unique_cliches,
        total_sentences=report.cliche_result.total_sentences,
        flagged_count=len(filtered_fs),
    )

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

    filtered_not_but = [nb for nb in report.not_but_result if nb.get("sentence", "") in target_text]

    return AuditReport(
        cliche_result=filtered_cliche,
        monotony_result=filtered_monotony,
        template_result=filtered_template,
        not_but_result=filtered_not_but,
        phrase_result=report.phrase_result,
        structural_repetition_result=report.structural_repetition_result,
        echo_result=report.echo_result,
    )


class PatchErrorKind:
    """Classify why a patch did not apply."""

    MALFORMED = "malformed"  # not an {id, replace} object, or `replace` is not text
    UNKNOWN_ID = "unknown_id"  # names no finding in the report
    DUPLICATE_ID = "duplicate_id"  # a second patch for a finding already patched
    NO_OP = "no_op"  # `replace` repeats the flagged text, before or after healing
    RESTATED_CONTEXT = "restated_context"  # healed away entirely: all copied context
    PROTECTED_SEQUENCE = "protected_sequence"  # clones text outside the target span


class PatchError(str):
    """An error string carrying its finding id and kind."""

    tid: int | None
    kind: str

    def __new__(cls, text: str, *, tid: int | None, kind: str) -> PatchError:
        error = super().__new__(cls, text)
        error.tid = tid
        error.kind = kind
        return error


def _coerce_id(raw: object) -> int | None:
    """Coerce a model-supplied finding id, rejecting booleans and fractions."""
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
    """Return the adjacent target bounds for each target id."""
    ordered = sorted(targets, key=lambda t: t.start)
    bounds: dict[int, tuple[int, int]] = {}
    for k, target in enumerate(ordered):
        previous_end = min(ordered[k - 1].end, target.start) if k else 0
        next_start = max(ordered[k + 1].start, target.end) if k + 1 < len(ordered) else len(draft)
        bounds[target.tid] = (max(previous_end, 0), min(next_start, len(draft)))
    return bounds


def _no_op_error(tid: int) -> PatchError:
    """Build the standard no-op patch error."""
    return PatchError(
        f"Error: the patch for id {tid} is a no-op — `replace` repeats the flagged text unchanged.",
        tid=tid,
        kind=PatchErrorKind.NO_OP,
    )


def apply_id_patches(draft: str, targets: Sequence[Target], patches: Sequence[Any]) -> tuple[str, list[PatchError]]:
    """Apply id-anchored replacements and return the updated draft and errors."""
    errors: list[PatchError] = []
    by_id = {t.tid: t for t in targets}
    id_range = f"1-{len(targets)}" if targets else "(none — the report has no numbered findings)"
    resolved: list[tuple[Target, str]] = []
    seen_ids: set[int] = set()
    logger.debug("Applying %d id-patches to draft (%d chars, %d targets)", len(patches), len(draft), len(targets))

    for i, p in enumerate(patches):
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

    out = draft
    heal_errors: list[PatchError] = []
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

    errors.extend(reversed(heal_errors))
    logger.debug("Patch application done: %d errors out of %d patches", len(errors), len(patches))
    return out, errors
