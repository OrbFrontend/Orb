"""Resolve image-generation targets and render images."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .adapters.base import ImageAdapter
from .contracts import (
    ImageGenerationError,
    ImageRequest,
    ImageResult,
    ProgressCallback,
    RenderTarget,
)
from .degrade import DROPPABLE_FIELDS, next_rung, trim

# Every rung strictly reduces what the request carries, so the ladder is finite on its
# own: two for the references -- the count the provider named, then none at all -- and
# one apiece for the optional fields, each of which can only be given up once. Derived
# rather than written down so adding a droppable field cannot silently cost the ladder
# a rung it needs.
MAX_DEGRADATIONS = 2 + len(DROPPABLE_FIELDS)


def _optional_flags(request: ImageRequest, target: RenderTarget) -> list[bool]:
    """Which of the sent references may be left out, positionally.

    Read off the slot the reference was resolved for, which already carries
    `required`: a ComfyUI graph's image inputs are required -- rendering one unfilled
    submits whatever filename the workflow was exported with -- and a cloud provider's
    are not, because there is always a plain generations endpoint one field away. So
    "can this backend degrade" is answered by data the target already declares, not by
    a branch on which backend it is.
    """
    required_by_slot = {tuple(slot["slot"]): bool(slot.get("required")) for slot in target.reference_slots if slot.get("slot")}
    return [not required_by_slot.get(tuple(reference.slot), False) for reference in request.references]


def _sending(request: ImageRequest) -> tuple[str, ...]:
    """Which droppable optional fields this attempt is actually carrying.

    Read off the request rather than declared, for the reason `_optional_flags` reads
    the slots: the ladder's bound is that every rung takes something away, and a field
    already cleared is one the next refusal must not be able to name again.
    """
    return tuple(name for name in DROPPABLE_FIELDS if str(getattr(request, name, "") or "").strip())


def _with_notes(result: ImageResult, notes: Sequence[str]) -> ImageResult:
    """The result, carrying whatever the ladder had to disclose.

    Prepended: a degradation explains the render the other notes then describe, and
    it is the one the user most needs to see first.
    """
    if not notes:
        return result
    info = dict(result.backend_info)
    info["notes"] = [*notes, *(info.get("notes") or [])]
    return replace(result, backend_info=info)


async def resolve_and_generate(
    adapter: ImageAdapter,
    request: ImageRequest,
    *,
    target: RenderTarget,
    progress: ProgressCallback | None = None,
) -> ImageResult:
    """Render `request` on the target the caller already resolved.

    `target` is required, and there is deliberately no `replay=`: both paths in
    `hooks.py` need answers off the target before the call, and a second way to
    reach one is how they come to disagree about replay precedence.

    A provider that refuses the request is asked once more with less of it -- fewer
    references, or without an optional field it named -- rather than failing the
    render, bounded by `MAX_DEGRADATIONS` and disclosed on the attachment. A refusal
    that is about neither, or a backend whose slots cannot be dropped, raises
    untouched.
    """
    attempt = request
    notes: list[str] = []
    # One attempt, plus at most `MAX_DEGRADATIONS` retries. `remaining` counting down
    # rather than a bare range is what makes the last pass re-raise instead of
    # degrading once more and falling out of the loop with nowhere to put the result.
    for remaining in range(MAX_DEGRADATIONS, -1, -1):
        try:
            return _with_notes(await adapter.generate(attempt, target=target, progress=progress), notes)
        except ImageGenerationError as exc:
            optional = _optional_flags(attempt, target)
            rung = (
                next_rung(exc, sent=len(attempt.references), droppable=sum(optional), sending=_sending(attempt))
                if remaining
                else None
            )
            if rung is None:
                raise
            attempt = replace(attempt, references=trim(attempt.references, optional, rung.keep))
            if rung.drop:
                attempt = replace(attempt, **{rung.drop: ""})
            notes.append(rung.note)
    raise AssertionError("unreachable: the final pass re-raises")  # pragma: no cover
