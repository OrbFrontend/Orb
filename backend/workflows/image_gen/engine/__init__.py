"""Narrow public facade for external image generation."""

from .adapters.external_comfy import CAPABILITIES, list_models, validate_connection
from .contracts import (
    ImageBackendCapabilities,
    ImageGenerationError,
    ImageRequest,
    ImageResult,
)
from .render import resolve_and_generate

__all__ = [
    "CAPABILITIES",
    "ImageBackendCapabilities",
    "ImageGenerationError",
    "ImageRequest",
    "ImageResult",
    "list_models",
    "resolve_and_generate",
    "validate_connection",
]
