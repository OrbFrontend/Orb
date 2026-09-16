"""Public image-generation engine facade."""

from . import health
from .contracts import (
    ImageGenerationError,
    ImageRequest,
    ImageResult,
    ProgressCallback,
    recorded_edge,
)
from .render import resolve_and_generate
from .router import comfy_adapter, get_adapter, list_sources

__all__ = [
    "ImageGenerationError",
    "ImageRequest",
    "ImageResult",
    "ProgressCallback",
    "comfy_adapter",
    "get_adapter",
    "health",
    "list_sources",
    "recorded_edge",
    "resolve_and_generate",
]
