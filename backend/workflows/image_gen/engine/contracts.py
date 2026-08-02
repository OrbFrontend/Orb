"""Engine-only contracts for the image generation workflow."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

# A render's progress reported back to the hook layer, which turns each stage into
# a phase label on the wire. Sync or async: a callback that only enqueues a string
# has no reason to be a coroutine.
ProgressCallback = Callable[[str, Mapping[str, Any]], Awaitable[None] | None]


async def emit(progress: ProgressCallback | None, stage: str, detail: Mapping[str, Any]) -> None:
    """Report one stage, awaiting the callback only when it returned an awaitable."""
    if not progress:
        return
    maybe = progress(stage, detail)
    if maybe is not None:
        await maybe


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
    """What will actually execute one render -- the dynamic tier.

    `ImageBackendCapabilities` above is the static tier ("can this backend
    ever?"); this is "what will this one do?", a per-graph question for ComfyUI.
    There is no `supports_references` -- it collapses into "`reference_slots` is
    non-empty".

    `notes` carries user-facing disclosure for a replay that could not be honoured
    exactly: substituting silently is the thing to avoid, and refusing outright is
    not the alternative.
    """

    source: str
    # comfy: the imported graph's id. cloud: "" -- there is no graph.
    target_id: str
    # comfy: the checkpoint filename. cloud: the model id.
    model: str
    supports_negative_prompt: bool
    supports_seed: bool
    supports_dimensions: bool
    # comfy: None, the graph decides. cloud: the replay's recorded size, else config.
    width: int | None
    height: int | None
    reference_slots: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedReference:
    """One reference image, already fetched, for one mapped `LoadImage` widget.

    `origin` names where the bytes came from in a form a later replay can re-fetch
    by (``"attachment:<id>"``, ``"character:<card id>"``). `digest` identifies the
    bytes *as sent*, so two slots resolving to one image upload once.

    `source_digest` identifies them *as fetched*, before the destination's mime/size
    policy touched them, and is the only one comparable across renders: a replay
    re-keyed onto another backend's slot converts differently and would fail a
    `digest` comparison for a reason that has nothing to do with the picture. It is
    what lets replay notice an origin's content changed underneath it -- a rehydrate
    on a seedless provider rewrites a row's bytes in place, under the same id.
    """

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


class ImageGenerationError(RuntimeError):
    """One caller-facing failure funnel for every render error, on any backend."""
