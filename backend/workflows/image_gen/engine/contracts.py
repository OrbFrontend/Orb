"""Engine-only contracts for the image generation workflow."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

from ...errors import WorkflowUserFacingError

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

    `reference_capacity` is the **ceiling**, where `reference_slots` is what the
    style actually switched on: a cloud provider's `max_references`, or the number
    of image inputs a ComfyUI graph declares. Zero when this target cannot carry a
    reference at all -- an unsupported provider, or a model off the allowlist.

    The pair is what lets a render tell a caller *why* somebody in frame got no
    likeness. Fewer slots than capacity is the style's own doing and the user can
    add a row; slots at capacity is the backend's ceiling and they cannot. Only the
    target knows either number, and the caller may not ask a preset directly
    without learning which backend it is talking to.
    """

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


class ImageGenerationError(WorkflowUserFacingError):
    """One caller-facing failure funnel for every render error, on any backend.

    User-facing by inheritance, which is what stops a provider's own explanation
    from being replaced with "see server logs" on the regenerate, reroll and
    rehydrate routes -- the streaming path already relayed it, so the same failed
    render used to read two different ways depending on which button was pressed.

    `kind` is the backend-independent shape of the failure -- was this about the
    credential, the rate limit, the server, or *what we sent*. `engine/degrade.py`
    reads it to decide whether a refusal is worth retrying one rung down, and it is
    declared here rather than on the cloud subclass so that question can be asked of
    any backend's error without knowing which backend raised it. A backend that does
    not classify leaves it "", which degrades to "do not retry".
    """

    kind: str = ""
