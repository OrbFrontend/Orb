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

# How long to wait before asking again after a refusal a rung answers.
#
# Nothing in the ladder is urgent -- every retry follows a request the provider has
# already turned down -- and a burst-sensitive limiter reads two calls in the same
# second as traffic rather than as a correction. Measured against Together AI
# 2026-09-06: the retry that carried the refit seed came back 429 twice in a row with
# no pause, and rendered on the third rung with one. Without this, the seed refit --
# which every Orb render on that provider needs, since Orb draws 64-bit seeds and the
# model bounds them at 2**31-1 -- turned a fixable 400 into a failed render.
RETRY_PAUSE_SECONDS = 2.0

# The wait after a 429 itself, which is the provider asking for exactly this. Longer
# than the ordinary pause, and applied to the **unchanged** request: a rate limit says
# nothing about what was sent.
RATE_LIMIT_PAUSE_SECONDS = 6.0

# How many times one render may wait out a rate limit. Bounded like every rung: two
# waits cover a burst, while a provider that is out of capacity for the next minute
# should reach the user as the failure it is rather than as a stall.
MAX_RATE_LIMIT_WAITS = 2


async def _pause(seconds: float) -> None:
    """Every wait the ladder takes, in one place, so a test can watch it decide to wait
    without waiting."""
    await asyncio.sleep(seconds)


# Two for the references -- the count the provider named, then none at all -- one
# apiece for the optional fields, each of which can only be given up once, and one each
# for a seed and a resolution the provider refused. Derived rather than written down so
# adding a droppable field cannot silently cost the ladder a rung it needs.
#
# The reference and field rungs strictly reduce what the request carries, which is what
# makes them finite on their own; the two refits instead only fire when they move the
# value into the range the refusal quoted, so the same refusal cannot ask twice. This
# count is the hard bound either way.
MAX_DEGRADATIONS = 4 + len(DROPPABLE_FIELDS)


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


def _describe(rung: Rung) -> str:
    """What the next attempt will do differently, for the log line."""
    if rung.seed is not None:
        return f"refitting the seed to {rung.seed}"
    if rung.size is not None:
        return f"refitting the size to {rung.size[0]}x{rung.size[1]}"
    if rung.drop:
        return f"dropping {rung.drop}"
    return f"keeping {rung.keep} of the reference images"


def _known_seed(known: Mapping[str, Any], seed: int) -> int:
    """`seed` folded into the bound this target has already been refused over."""
    high = known.get("seed_high")
    low = known.get("seed_low", 0)
    if not isinstance(high, int) or isinstance(high, bool) or not isinstance(low, int) or isinstance(low, bool):
        return seed
    return fold_seed_into(seed, low, high)


def _known_size(known: Mapping[str, Any], size: tuple[int, int] | None) -> tuple[int, int] | None:
    """The size this target rendered the last time it was asked for `size`, if any."""
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
    known: Mapping[str, Any] | None = None,
) -> ImageResult:
    """Render `request` on the target the caller already resolved.

    `target` is required, and there is deliberately no `replay=`: both paths in
    `hooks.py` need answers off the target before the call, and a second way to
    reach one is how they come to disagree about replay precedence.

    A provider that refuses the request is asked once more with less of it -- fewer
    references, or without an optional field it named -- rather than failing the
    render, bounded by `MAX_DEGRADATIONS` and disclosed on the attachment. A refusal
    that quotes a seed range instead is answered by refitting the seed into it, which
    gives up nothing and so discloses nothing; the adapter records the seed it ended
    up sending. A refusal that is about none of those, or a backend whose slots
    cannot be dropped, raises untouched.

    Every retry waits first (`RETRY_PAUSE_SECONDS`), and a 429 is waited out rather
    than degraded -- twice at most, on the unchanged request. A ladder that answers
    instantly reads to a burst-sensitive limiter as traffic, and the 429 it earns then
    ends the render, so pacing is what makes the rungs above reachable at all rather
    than a courtesy.

    `known` is what earlier refusals already revealed about this target, applied
    *before* the first attempt so the ladder does not have to rediscover it. That is
    not only latency: the retry lands within milliseconds of the refusal, and a
    provider with a burst-sensitive limiter answers the second call with a 429 that
    stops the ladder dead -- so re-probing every render turns a fixable request into a
    failed one. Whatever the ladder learns anyway comes back on
    `backend_info["learned"]`, for the caller to store.

    A stale `known` costs nothing structural: it is applied, the provider refuses it
    like any other request, and the ladder relearns and overwrites. It can never be
    load-bearing, only a head start.
    """
    attempt = request
    # The target is rebound alongside the request, because the resolution lives on it
    # rather than on `ImageRequest` -- a replay has to pin the size the stored image was
    # generated at, so the target is what already reads the stored record.
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
    # One attempt, plus at most `MAX_DEGRADATIONS` retries that change the request and
    # `MAX_RATE_LIMIT_WAITS` that only wait. Counting down rather than iterating a range
    # is what makes the last pass re-raise instead of degrading once more and falling
    # out of the loop with nowhere to put the result.
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
                # Waited out rather than answered. The request is sent again exactly as
                # it was -- there is nothing in it to fix -- and the degradation budget
                # is untouched, so a burst that lands between two rungs cannot cost the
                # render the rung it was about to take.
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
            # The refusal that caused a rung is otherwise the one thing nobody can
            # see. A degraded render that succeeds reports only the note -- and a seed
            # refit has none by design -- while one that goes on to fail reports only
            # its *last* error, so a 400 answered here and a 429 on the retry read as a
            # bare rate limit with no hint that anything preceded it. The provider's
            # own words about every attempt belong in the log, whichever way it ends.
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
                # `sizes` accumulates across rungs; the scalar bounds replace, so the
                # most recent thing the provider said about one is what is kept.
                if key == "sizes" and isinstance(value, Mapping):
                    learned["sizes"] = {**learned.get("sizes", {}), **value}
                else:
                    learned[key] = value
            remaining -= 1
            await _pause(RETRY_PAUSE_SECONDS)
