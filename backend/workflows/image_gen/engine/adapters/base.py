"""The image-backend adapter protocol.

Mirrors the TTS split (``tts/engine/base.py`` + ``router.py``) with one difference:
an image adapter binds a config at construction rather than taking one per call.
Every entry point already took ``config`` first, so binding it removes an argument
rather than adding a concept.
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
        `label` names what is actually rendering ("xAI (Grok)"), which belongs on a
        stored attachment and in a progress line.
        """
        return self.display_name

    @abstractmethod
    def resolve_target(self, style: Mapping[str, Any], replay: Mapping[str, Any] | None) -> RenderTarget:
        """Pick what will execute, for a fresh render or a replay of a stored one.

        A fresh render follows the style; a replay follows what the stored image
        recorded, because reroll and rehydrate promise the *same* parameters.
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
    def readiness(self) -> dict:
        """``{"ready", "reason", "detail"}`` from the saved config alone, no I/O.

        Deliberately not a health probe: the tools-panel card renders on every open,
        and making that wait on a remote server trades a fast answer for a slow one.
        """

    async def list_models(self) -> list[str]:
        return []
