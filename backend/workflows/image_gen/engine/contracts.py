"""Engine-only contracts for the image generation workflow."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypedDict


class ImageBackendCapabilities(TypedDict):
    can_generate: bool
    can_list_models: bool
    can_install_curated_models: bool
    managed_runtime: bool


@dataclass(frozen=True)
class ResolvedReference:
    """One reference image, already fetched, for one mapped `LoadImage` widget.

    `origin` names *where the bytes came from* in a form a later replay can
    re-fetch by (``"attachment:<id>"``, ``"character:<card id>"``); `digest`
    identifies the bytes, so two slots resolving to the same image upload once.
    """

    slot: tuple[str, str]
    source: str
    data: bytes
    mime: str
    origin: str
    digest: str


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    negative_prompt: str
    seed: int
    style_id: str
    recipe_id: str | None = None
    width: int | None = None
    height: int | None = None
    timeout_seconds: float = 180.0
    references: tuple[ResolvedReference, ...] = ()


@dataclass(frozen=True)
class ImageResult:
    image_bytes: bytes
    mime: str
    backend_info: Mapping[str, Any]


class ImageGenerationError(RuntimeError):
    """One caller-facing failure funnel for every ComfyUI render error."""
