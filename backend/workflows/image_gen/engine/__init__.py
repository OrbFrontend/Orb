"""Public image-generation engine facade."""

from . import health
from .comfy_client import ComfyUnreachableError
from .contracts import ImageGenerationError, ImageRequest, ImageResult, ProgressCallback, recorded_edge
from .render import resolve_and_generate
from .router import comfy_adapter, get_adapter, list_sources
