"""The image-backend adapter protocol.

Mirrors the TTS split (``tts/engine/base.py`` + ``router.py``) with one
deliberate difference: an image adapter binds a config at construction rather
than taking one per call. Every image entry point already took ``config`` first,
so binding it once removes the argument instead of adding a concept.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

from ..contracts import (
    ImageBackendCapabilities,
    ImageRequest,
    ImageResult,
    ProgressCallback,
)
from ..target import RenderTarget


class ImageAdapter(ABC):
    """One image backend, bound to the configuration it will render with."""

    source_id: ClassVar[str]
    display_name: ClassVar[str]
    capabilities: ClassVar[ImageBackendCapabilities]

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = config

    @property
    def label(self) -> str:
        """What this *configured* adapter calls itself to the user.

        `display_name` names the backend for the source picker ("Cloud API");
        `label` names the thing actually rendering ("xAI (Grok)"), which is what
        belongs on a stored attachment and in a progress line. They coincide
        wherever a backend is only ever itself.
        """
        return self.display_name

    @abstractmethod
    def resolve_target(self, style: Mapping[str, Any], replay: Mapping[str, Any] | None) -> RenderTarget:
        """Pick what will execute, for a fresh render or for replaying a stored one.

        A fresh render follows the style (its pins, else the source-level
        selection). A replay follows what the stored image recorded, because
        reroll and rehydrate promise the *same* image parameters -- resolving
        through the style instead would silently re-render an old attachment on
        whatever the style points at today.
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
        """Prove this configuration can render, **without submitting anything**.

        A Test-connection button that bills the user is unacceptable, so a cloud
        adapter answers this from its model-listing endpoint alone.
        """

    @abstractmethod
    def readiness(self) -> dict:
        """``{"ready", "reason", "detail"}`` from the saved config alone, no I/O.

        Deliberately not a health probe: the tools-panel card renders on every
        panel open, and making that wait on a remote server would trade a fast
        honest answer for a slow one.
        """

    async def list_models(self) -> list[str]:
        return []
