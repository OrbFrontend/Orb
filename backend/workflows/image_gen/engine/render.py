"""Resolve image-generation targets and render images."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from .adapters.base import ImageAdapter
from .contracts import (
    ImageGenerationError,
    ImageRequest,
    ImageResult,
    ProgressCallback,
    RenderTarget,
    fold_seed_into,
)
from .degrade import DROPPABLE_FIELDS, RATE_LIMITED, Rung, next_rung, resized_note, trim

logger = logging.getLogger(__name__)

# Pace fallback calls so providers do not interpret them as a burst.
RETRY_PAUSE_SECONDS = 2.0

# A 429 retries the unchanged request, without spending a degradation rung.
RATE_LIMIT_PAUSE_SECONDS = 6.0

MAX_RATE_LIMIT_WAITS = 2


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


# Two reference fallbacks, one per droppable field, and one each for seed and size.
MAX_DEGRADATIONS = 4 + len(DROPPABLE_FIELDS)


def _optional_flags(request: ImageRequest, target: RenderTarget) -> list[bool]:
    required_by_slot = {tuple(slot["slot"]): bool(slot.get("required")) for slot in target.reference_slots if slot.get("slot")}
    return [not required_by_slot.get(tuple(reference.slot), False) for reference in request.references]


def _sending(request: ImageRequest) -> tuple[str, ...]:
    return tuple(name for name in DROPPABLE_FIELDS if str(getattr(request, name, "") or "").strip())


def _describe(rung: Rung) -> str:
    if rung.seed is not None:
        return f"refitting the seed to {rung.seed}"
    if rung.size is not None:
        return f"refitting the size to {rung.size[0]}x{rung.size[1]}"
    if rung.drop:
        return f"dropping {rung.drop}"
    return f"keeping {rung.keep} of the reference images"


def _known_seed(known: Mapping[str, Any], seed: int) -> int:
    high = known.get("seed_high")
    low = known.get("seed_low", 0)
    if not isinstance(high, int) or isinstance(high, bool) or not isinstance(low, int) or isinstance(low, bool):
        return seed
    return fold_seed_into(seed, low, high)


def _known_size(known: Mapping[str, Any], size: tuple[int, int] | None) -> tuple[int, int] | None:
    sizes = known.get("sizes")
    stored = sizes.get(f"{size[0]}x{size[1]}") if isinstance(sizes, Mapping) and size else None
    if not isinstance(stored, str):
        return None
    width, _, height = stored.partition("x")
    if not width.isdigit() or not height.isdigit():
        return None
    fitted = (int(width), int(height))
    return fitted if size and fitted != size else None


def _with_notes(result: ImageResult, notes: Sequence[str]) -> ImageResult:
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
    known: Mapping[str, Any] | None = None,
) -> ImageResult:
    """Generate once, then apply bounded, disclosed fallbacks to request refusals."""
    attempt = request
    current = target
    notes: list[str] = []
    learned: dict[str, Any] = {}

    remembered = known or {}
    seeded = _known_seed(remembered, attempt.seed)
    if seeded != attempt.seed:
        attempt = replace(attempt, seed=seeded)
    sized = _known_size(remembered, (current.width, current.height) if current.width and current.height else None)
    if sized is not None and current.width and current.height:
        # Disclosed exactly as the ladder discloses it. A resize the user is told about
        # only on the render that discovered it, and silently thereafter, is worse than
        # one they are never told about: it looks like the setting works.
        notes.append(resized_note((current.width, current.height), sized))
        current = replace(current, width=sized[0], height=sized[1])
    remaining, waits = MAX_DEGRADATIONS, MAX_RATE_LIMIT_WAITS
    while True:
        try:
            result = _with_notes(await adapter.generate(attempt, target=current, progress=progress), notes)
            if not learned:
                return result
            # Only on success. A bound read off a refusal that was never followed by a
            # render is a guess about what would have worked, and storing it would let
            # one bad parse pin every later render to a size nothing ever drew.
            return replace(result, backend_info={**result.backend_info, "learned": learned})
        except ImageGenerationError as exc:
            if getattr(exc, "kind", "") == RATE_LIMITED:
                if not waits:
                    raise
                waits -= 1
                logger.info("image render rate-limited (%s); waiting %.0fs", exc, RATE_LIMIT_PAUSE_SECONDS)
                await _pause(RATE_LIMIT_PAUSE_SECONDS)
                continue
            optional = _optional_flags(attempt, current)
            rung = (
                next_rung(
                    exc,
                    sent=len(attempt.references),
                    droppable=sum(optional),
                    seed=attempt.seed,
                    size=(current.width, current.height) if current.width and current.height else None,
                    sending=_sending(attempt),
                )
                if remaining
                else None
            )
            if rung is None:
                raise
            logger.info("image render refused (%s); retrying by %s", exc, _describe(rung))
            attempt = replace(attempt, references=trim(attempt.references, optional, rung.keep))
            if rung.drop:
                attempt = replace(attempt, **{rung.drop: ""})
            if rung.seed is not None:
                attempt = replace(attempt, seed=rung.seed)
            if rung.size is not None:
                current = replace(current, width=rung.size[0], height=rung.size[1])
            if rung.note:
                notes.append(rung.note)
            for key, value in rung.learned.items():
                if key == "sizes" and isinstance(value, Mapping):
                    learned["sizes"] = {**learned.get("sizes", {}), **value}
                else:
                    learned[key] = value
            remaining -= 1
            await _pause(RETRY_PAUSE_SECONDS)
