"""Narrow public facade for image generation."""

from .adapters.base import ImageAdapter
from .adapters.external_comfy import ExternalComfyAdapter
from .comfy_client import invalidate_object_info
from .contracts import (
    ImageBackendCapabilities,
    ImageGenerationError,
    ImageRequest,
    ImageResult,
    ProgressCallback,
    ResolvedReference,
)
from .graph import has_graph
from .render import resolve_and_generate, resolve_render_target
from .router import comfy_adapter, get_adapter, list_sources
from .target import RenderTarget

__all__ = [
    "ExternalComfyAdapter",
    "ImageAdapter",
    "ImageBackendCapabilities",
    "ImageGenerationError",
    "ImageRequest",
    "ImageResult",
    "ProgressCallback",
    "RenderTarget",
    "ResolvedReference",
    "comfy_adapter",
    "get_adapter",
    "has_graph",
    "invalidate_object_info",
    "list_sources",
    "resolve_and_generate",
    "resolve_render_target",
]
