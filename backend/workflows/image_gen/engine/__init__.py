"""Narrow public facade for external image generation."""

from .adapters.external_comfy import (
    CAPABILITIES,
    list_models,
    node_roles,
    validate_connection,
)
from .comfy_client import ProgressCallback, invalidate_object_info
from .contracts import (
    ImageBackendCapabilities,
    ImageGenerationError,
    ImageRequest,
    ImageResult,
)
from .graph import graph_has_negative, has_graph
from .render import RenderTarget, resolve_and_generate, resolve_render_target

__all__ = [
    "CAPABILITIES",
    "ImageBackendCapabilities",
    "ImageGenerationError",
    "ImageRequest",
    "ImageResult",
    "ProgressCallback",
    "RenderTarget",
    "graph_has_negative",
    "has_graph",
    "invalidate_object_info",
    "list_models",
    "node_roles",
    "resolve_and_generate",
    "resolve_render_target",
    "validate_connection",
]
