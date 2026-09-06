"""Engine-only contracts for the image generation workflow."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypedDict

from ...toolkit import WorkflowUserFacingError

ProgressCallback = Callable[[str, Mapping[str, Any]], Awaitable[None] | None]


async def emit(progress: ProgressCallback | None, stage: str, detail: Mapping[str, Any]) -> None:
    """Report one stage, awaiting the callback only when it returned an awaitable."""
    if not progress:
        return
    maybe = progress(stage, detail)
    if maybe is not None:
        await maybe


def recorded_edge(value: Any) -> int | None:
    """One recorded pixel edge, or None when it is absent or not a positive whole number.

    `isinstance(True, int)` is True, so bools are excluded by hand -- a hand-edited
    record must not resolve to a 1-pixel edge.

    Shared rather than copied per caller: the adapters resolve a replayed size with it
    and the hook grades a recorded one, and the copy that drifted first lost `> 0`.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def fold_seed_into(seed: int, low: int, high: int) -> int:
    """`seed` folded into the inclusive range `[low, high]`, or unchanged where that
    is not a range at all.

    Backends disagree about how large a seed may be, and each says so in its own way:
    a ComfyUI node declares `min`/`max` in `/object_info`, while a cloud provider
    quotes its bound in the refusal. *How* the bound is learned differs; "make this
    seed fit" does not, so it lives here rather than once per caller -- the copy that
    drifted first would be the one whose recorded seed no longer reproduces its image.

    Folded rather than clamped: clamping would draw every out-of-range seed as the
    same image, and folding is idempotent, so the seed Orb records still reproduces
    this render when it is replayed through the same bound.
    """
    if high < low:
        return seed
    # Negative seeds are legal where a backend offers them and nobody wants them, so
    # the fold starts at zero wherever that is still inside the range.
    low = max(low, 0) if high >= 0 else low
    if low <= seed <= high:
        return seed
    return low + (seed - low) % (high - low + 1)


def ratio_distance(target: float, ratio: float | None) -> float:
    """Return logarithmic distance between two aspect ratios."""
    return abs(math.log(target) - math.log(ratio)) if ratio and ratio > 0 else math.inf


class ImageBackendCapabilities(TypedDict):
    """What a backend *can ever* do -- static, per adapter class.

    Drives the UI (graph importer? model dropdown? resolution picker?) and the
    permanent-gap disclosure in the settings panel. The dynamic tier -- what one
    resolved style/graph/model will actually honour -- lives on `RenderTarget`.
    """

    can_generate: bool
    can_list_models: bool
    can_install_curated_models: bool
    managed_runtime: bool
    supports_negative_prompt: bool
    supports_seed: bool
    supports_dimensions: bool
    supports_references: bool


@dataclass(frozen=True)
class RenderTarget:
    """Dynamic settings for one image render."""

    source: str
    target_id: str
    model: str
    supports_negative_prompt: bool
    supports_seed: bool
    supports_dimensions: bool
    width: int | None
    height: int | None
    reference_slots: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()
    quality: str = ""
    reference_source: str = ""
    reference_capacity: int = 0
    reference_template: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedReference:
    """A fetched image for one mapped LoadImage widget."""

    slot: tuple[str, str]
    source: str
    data: bytes
    mime: str
    origin: str
    digest: str
    source_digest: str = ""

    def record(self) -> dict:
        """The replay record, in the shape `refetch_references` reads back."""
        return {
            "slot": list(self.slot),
            "source": self.source,
            "origin": self.origin,
            "digest": self.digest,
            "source_digest": self.source_digest,
        }


@dataclass(frozen=True)
class ImageRequest:
    """What to draw. **Not** what will draw it -- that is `RenderTarget`.

    Resolution rides the target for the same reason `model` does: a replay must
    pin the resolution the stored image was generated at, and the target is what
    already reads the stored record. Two homes for it is how the fresh path and
    the reroll path come to disagree about replay precedence.
    """

    prompt: str
    negative_prompt: str
    seed: int
    style_id: str
    timeout_seconds: float = 180.0
    references: tuple[ResolvedReference, ...] = ()


@dataclass(frozen=True)
class ImageResult:
    image_bytes: bytes
    mime: str
    backend_info: Mapping[str, Any]


class ImageGenerationError(WorkflowUserFacingError):
    """Caller-facing error for image generation."""

    kind: str = ""
