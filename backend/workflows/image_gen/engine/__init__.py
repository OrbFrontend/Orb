"""Narrow public facade for image generation.

Only what crosses out of `engine/`: the hook layer's render seam, the adapter
lookups the query actions dispatch through, and the error type every caller
funnels on. Adapters, capabilities and the graph helpers are imported from their
own modules -- re-exporting them here would advertise a surface nothing uses.
"""

from .contracts import (
    ImageGenerationError,
    ImageRequest,
    ImageResult,
    ProgressCallback,
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
    "list_sources",
    "resolve_and_generate",
]
