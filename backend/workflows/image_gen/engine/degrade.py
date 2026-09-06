"""Choose bounded fallbacks when a provider rejects optional fields."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    ImageGenerationError,
    ResolvedReference,
    fold_seed_into,
    ratio_distance,
)

# Provider read the request and rejected it.
REQUEST_REFUSED = "request"

# A rate limit can be retried unchanged; it must not consume a degradation rung.
RATE_LIMITED = "rate_limit"

_IMAGE_WORDS = ("image", "images", "image_url", "imagedataurls", "reference")

_INTEGERS = re.compile(r"\d+")

_SEED_BETWEEN = re.compile(r"between\s+(-?\d+)\s+and\s+(-?\d+)")

_SEED_CEILING = re.compile(
    r"(?:less than or equal to|at most|no (?:greater|larger|more) than|maximum(?: of)?"
    r"|(?P<exclusive>less than|below|under))\s+(?P<bound>-?\d+)"
)

_SEED_TOKEN = re.compile(r"(?<![a-z0-9_])seed(?![a-z0-9_])")

_AREA_RANGE = re.compile(r"(\d+)\s*[x×*]\s*(\d+)\s*(?:to|and|-|–)\s*(\d+)\s*[x×*]\s*(\d+)")

_SIZE_PAIR = re.compile(r"(?<![\d.])(\d{2,5})\s*[x×*]\s*(\d{2,5})(?![\d.])")

_SIZE_WORDS = ("dimension", "area", "resolution", "width", "height", "pixel", "size")

# Prevent byte counts and IDs from being interpreted as reference counts.
_PLAUSIBLE_COUNT = 64

DROPPABLE_FIELDS: dict[str, str] = {
    "negative_prompt": "this model does not take a negative prompt, so it was rendered without one",
}

_FIELD_TOKENS = {name: re.compile(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])") for name in DROPPABLE_FIELDS}


# Bounds learned from a refusal; capability claims are intentionally not persisted.
LEARNABLE = ("seed_high", "seed_low", "sizes")


@dataclass(frozen=True)
class Rung:
    """One fallback attempt and the disclosure it requires."""

    keep: int
    note: str = ""
    drop: str = ""
    seed: int | None = None
    size: tuple[int, int] | None = None
    learned: Mapping[str, Any] = field(default_factory=dict)


def _mentions_image(message: str) -> bool:
    lowered = message.lower()
    return any(word in lowered for word in _IMAGE_WORDS)


def _named_field(message: str, sending: Sequence[str]) -> str:
    lowered = message.lower()
    return next((name for name in sending if name in _FIELD_TOKENS and _FIELD_TOKENS[name].search(lowered)), "")


def _named_limit(message: str, *, sent: int) -> int | None:
    candidates = [
        value for raw in _INTEGERS.findall(message) for value in (int(raw),) if 0 < value < sent and value <= _PLAUSIBLE_COUNT
    ]
    return max(candidates) if candidates else None


def _size_key(size: tuple[int, int]) -> str:
    return f"{size[0]}x{size[1]}"


def resized_note(size: tuple[int, int], fitted: tuple[int, int]) -> str:
    return f"this model does not render at {_size_key(size)}, so it was rendered at {_size_key(fitted)}"


def _seed_range(message: str) -> tuple[int, int] | None:
    pair = _SEED_BETWEEN.search(message)
    if pair:
        low, high = int(pair.group(1)), int(pair.group(2))
        return (low, high) if low <= high else None
    ceiling = _SEED_CEILING.search(message)
    if ceiling is None:
        return None
    high = int(ceiling.group("bound"))
    return (0, high - 1 if ceiling.group("exclusive") else high)


def _refit_size(message: str, size: tuple[int, int]) -> tuple[int, int] | None:
    lowered = message.lower()
    if not any(word in lowered for word in _SIZE_WORDS):
        return None
    window = _AREA_RANGE.search(message)
    if window is not None:
        return _rescaled(size, int(window.group(1)) * int(window.group(2)), int(window.group(3)) * int(window.group(4)))
    return _nearest_offered(message, size)


def _rescaled(size: tuple[int, int], low: int, high: int) -> tuple[int, int] | None:
    width, height = size
    area = width * height
    if low > high or area <= 0 or low <= area <= high:
        return None
    scale = math.sqrt(math.sqrt(low * high) / area)
    fitted = (max(1, round(width * scale)), max(1, round(height * scale)))
    return fitted if fitted != size else None


def _nearest_offered(message: str, size: tuple[int, int]) -> tuple[int, int] | None:
    offered = [(int(w), int(h)) for w, h in _SIZE_PAIR.findall(message)]
    choices = [(w, h) for w, h in offered if w > 0 and h > 0]
    if len(choices) < 2 or size[1] <= 0 or size in choices:
        return None
    target = size[0] / size[1]
    return min(choices, key=lambda c: (ratio_distance(target, c[0] / c[1]), abs(c[0] * c[1] - size[0] * size[1])))


def next_rung(
    exc: Exception,
    *,
    sent: int,
    droppable: int,
    seed: int = 0,
    size: tuple[int, int] | None = None,
    sending: Sequence[str] = (),
) -> Rung | None:
    if not isinstance(exc, ImageGenerationError) or getattr(exc, "kind", "") != REQUEST_REFUSED:
        return None
    message = str(exc)
    if _SEED_TOKEN.search(message.lower()):
        bounds = _seed_range(message)
        if bounds is not None:
            refit = fold_seed_into(seed, *bounds)
            if refit != seed:
                return Rung(keep=sent, seed=refit, learned={"seed_low": bounds[0], "seed_high": bounds[1]})
    fitted = _refit_size(message, size) if size is not None else None
    if fitted is not None and size is not None:
        return Rung(
            keep=sent,
            note=resized_note(size, fitted),
            size=fitted,
            learned={"sizes": {_size_key(size): _size_key(fitted)}},
        )
    named = _named_field(message, sending)
    if named:
        return Rung(keep=sent, note=DROPPABLE_FIELDS[named], drop=named)
    if sent <= 0 or droppable <= 0:
        return None
    if not _mentions_image(message):
        return None
    limit = _named_limit(message, sent=sent)
    if limit is not None and limit >= sent - droppable:
        dropped = sent - limit
        return Rung(
            keep=limit,
            note=(
                f"this model accepts {limit} reference image{'' if limit == 1 else 's'}, "
                f"so {dropped} of the {sent} sent {'was' if dropped == 1 else 'were'} left out"
            ),
        )
    keep = sent - droppable
    return Rung(
        keep=keep,
        note=(
            "this model would not take the reference images, so it was rendered from the prompt alone"
            if keep == 0
            else f"this model would not take {sent - keep} of the reference images, so they were left out"
        ),
    )


def trim(references: Sequence[ResolvedReference], optional: Sequence[bool], keep: int) -> tuple[ResolvedReference, ...]:
    budget = max(0, keep - sum(1 for is_optional in optional if not is_optional))
    surviving: list[ResolvedReference] = []
    for reference, is_optional in zip(references, optional):
        if not is_optional:
            surviving.append(reference)
            continue
        if budget > 0:
            surviving.append(reference)
            budget -= 1
    return tuple(surviving)
