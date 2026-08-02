"""Engine-only contracts for the image generation workflow."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

# A render's progress reported back to the hook layer, which turns each stage into
# a phase label on the wire. Sync or async: a callback that only enqueues a string
# has no reason to be a coroutine.
ProgressCallback = Callable[[str, Mapping[str, Any]], Awaitable[None] | None]


class ImageBackendCapabilities(TypedDict):
    """What a backend *can ever* do -- static, per adapter class.

    The static tier of a two-tier split. This drives the UI (show a graph
    importer? a model dropdown? a resolution picker?) and the permanent-gap
    disclosure in the settings panel. What one resolved style/graph/model will
    actually honour is the dynamic tier, and lives on `RenderTarget`: for ComfyUI
    "supports a negative prompt" and "takes reference images" are per-*graph*
    facts, not per-backend ones.
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
class ResolvedReference:
    """One reference image, already fetched, for one mapped `LoadImage` widget.

    `origin` names *where the bytes came from* in a form a later replay can
    re-fetch by (``"attachment:<id>"``, ``"character:<card id>"``); `digest`
    identifies the bytes *as sent*, so two slots resolving to the same image
    upload once.

    `source_digest` identifies the bytes *as fetched*, before the destination's
    mime/size policy touched them. The two differ whenever a conversion ran, and
    only this one is comparable across renders: a replay that re-keys a ComfyUI
    reference onto a cloud slot normalizes it differently and would fail a
    `digest` comparison for a reason that has nothing to do with the picture. It
    is what lets replay notice that an origin's *content* changed underneath it
    -- a rehydrate on a seedless provider rewrites a row's bytes in place, under
    the same id.
    """

    slot: tuple[str, str]
    source: str
    data: bytes
    mime: str
    origin: str
    digest: str
    source_digest: str = ""


@dataclass(frozen=True)
class ImageRequest:
    """What to draw. **Not** what will draw it -- that is `RenderTarget`.

    Resolution deliberately does not live here. It rides the target for the same
    reason `model` does: a replay must pin the resolution the stored image was
    generated at, and the target is what already reads the stored record. Two
    places to put a resolution is how the fresh-render path and the reroll path
    end up disagreeing about replay precedence.
    """

    prompt: str
    negative_prompt: str
    seed: int
    style_id: str
    recipe_id: str | None = None
    timeout_seconds: float = 180.0
    references: tuple[ResolvedReference, ...] = ()


@dataclass(frozen=True)
class ImageResult:
    image_bytes: bytes
    mime: str
    backend_info: Mapping[str, Any]


class ImageGenerationError(RuntimeError):
    """One caller-facing failure funnel for every render error, on any backend."""
