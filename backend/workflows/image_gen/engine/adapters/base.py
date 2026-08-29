"""The image-backend adapter protocol.

Mirrors the TTS split (``tts/engine/base.py`` + ``router.py``) with one difference:
an image adapter binds a config *and a style* at construction rather than taking
them per call. Every entry point already took ``config`` first, so binding it
removes an argument rather than adding a concept; the style rides along because
"which backend, which model, at what size" is a property of the style, and an
adapter answering about one style while rendering another is the bug this fixes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from ...config import active_style
from ..contracts import (
    ImageBackendCapabilities,
    ImageRequest,
    ImageResult,
    ProgressCallback,
    RenderTarget,
    recorded_edge,
)


class ImageAdapter(ABC):
    """One image backend, bound to the configuration and style it will render with."""

    source_id: ClassVar[str]
    display_name: ClassVar[str]
    capabilities: ClassVar[ImageBackendCapabilities]

    def __init__(self, config: Mapping[str, Any], style: Mapping[str, Any] | None = None) -> None:
        self.config = config
        self.style = style if style is not None else active_style(config)

    @property
    def label(self) -> str:
        """What this *configured* adapter calls itself to the user.

        `display_name` names the backend for the source picker ("Cloud API");
        `label` names what is actually rendering ("xAI (Grok)"), which belongs on a
        stored attachment and in a progress line.
        """
        return self.display_name

    @abstractmethod
    def resolve_target(self, replay: Mapping[str, Any] | None) -> RenderTarget:
        """Pick what will execute, for a fresh render or a replay of a stored one.

        A fresh render follows the bound style; a replay follows what the stored
        image recorded, because reroll and rehydrate promise the *same* parameters.

        Deliberately takes no style: the adapter is bound to one, and a second way
        to supply it is a second thing that can disagree with the router's choice --
        which is the bug that made a rehydrate render on the wrong backend.
        """

    @abstractmethod
    async def generate(
        self,
        request: ImageRequest,
        *,
        target: RenderTarget,
        progress: ProgressCallback | None = None,
    ) -> ImageResult: ...

    @abstractmethod
    async def validate_connection(self, *, allow_cached: bool = False) -> dict:
        """Prove this configuration can render, **without submitting anything**. A
        Test-connection button that bills the user is unacceptable, so a cloud
        adapter answers from its model-listing endpoint alone."""

    @abstractmethod
    def readiness(self, model: str = "") -> dict:
        """``{"ready", "reason", "detail"}`` about the bound style, no I/O.

        Deliberately not a health probe: the tools-panel card renders on every open,
        and making that wait on a remote server trades a fast answer for a slow one.

        `model` asks the question about a specific model rather than the configured
        one, so a replay is judged on what the stored image recorded. An adapter that
        does not pick models per render may ignore it.
        """

    async def list_models(self) -> list[str]:
        return []


def replayed_target(replay: Mapping[str, Any] | None, *, model: str, width: int, height: int) -> tuple[str, int, int]:
    """What a stored image recorded, falling back to what the bound style says now.

    The two facts every backend replays identically -- which model drew it, and at
    what size. One function rather than a copy per adapter, because they must not
    drift: reading today's setting at replay time is the silent substitution reroll
    and rehydrate exist to avoid, and it is only ever noticed as "the restored image
    is the wrong shape".

    A recorded model of `""` or `None` falls through rather than pinning nothing: it
    means the original ran a target carrying its own -- a self-contained ComfyUI graph
    with its loaders inside it. The size is taken only as a *pair*, since half a
    recorded resolution describes no image.

    What is *not* here is the graph id: that is ComfyUI's alone, and it is the one pin
    that can be gone from the config, which the caller has to disclose.
    """
    if not replay:
        return model, width, height
    stored_model = replay.get("backend_model")
    if isinstance(stored_model, str) and stored_model:
        model = stored_model
    stored_w, stored_h = recorded_edge(replay.get("width")), recorded_edge(replay.get("height"))
    if stored_w is not None and stored_h is not None:
        width, height = stored_w, stored_h
    return model, width, height


def replayed_text(replay: Mapping[str, Any] | None, key: str, current: str) -> str:
    """One recorded string setting, falling back to what the bound style says now.

    Membership is not the test and truthiness is not either: `""` is a real recorded
    value for both settings this serves -- "the provider's default quality", "no
    reference" -- while a *typed* absence is what an attachment made on a backend
    with no such setting records, and what every attachment predating the record has.
    So the rule is "a string wins, anything else falls through".
    """
    stored = (replay or {}).get(key)
    return stored if isinstance(stored, str) else current


def replayed_reference_source(
    replay: Mapping[str, Any] | None,
    current: str,
    *,
    slots: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Where the stored render drew its reference from, falling back to `current`.

    The source moved onto the style, where it is editable after the fact, so a
    rehydrate replaying it off the style would quietly reproduce a *different*
    picture -- the one failure a rehydrate is not allowed to have. Turning
    references off in settings used to re-render an evicted image from the prompt
    alone and overwrite the row with it.

    The two backends record the fact differently, which is what `slots` selects:

    * **Cloud** (no `slots`) answers from the stored scalar, and **a string wins,
      not a truthy one**: `""` is a real recorded value -- "this render sent no
      reference" -- so a record carrying it is authoritative. Only a record with no
      scalar at all, made before the field existed, falls back to `current`.
    * **ComfyUI** (`slots` = the graph's declared reference slots) answers from the
      per-reference records instead, because the scalar says nothing about *which*
      graph it described. The record names the node input each reference filled, and
      that re-keys onto the declared list directly. Every reference on one render
      shares a source, so the first that lands answers for all; a record whose slots
      land nowhere is about some other graph and yields `current` rather than
      rendering blind.
    """
    if not replay:
        return current
    recorded = replay.get("reference_source")
    if slots is None:
        return recorded if isinstance(recorded, str) else current

    declared = {
        (str(slot[0]), str(slot[1]))
        for entry in slots or ()
        for slot in (entry.get("slot"),)
        if isinstance(slot, (list, tuple)) and len(slot) == 2
    }
    entries = replay.get("references")
    return next(
        (
            source
            for entry in (entries if isinstance(entries, (list, tuple)) else ())
            if isinstance(entry, Mapping) and _slot_key(entry.get("slot")) in declared
            for source in (str(entry.get("source") or ""),)
            if source
        ),
        current,
    )


def _slot_key(slot: Any) -> tuple[str, str] | None:
    return (str(slot[0]), str(slot[1])) if isinstance(slot, (list, tuple)) and len(slot) == 2 else None
