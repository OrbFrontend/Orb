"""Image backend adapters."""

from .base import ImageAdapter
from .external_comfy import ExternalComfyAdapter

__all__ = ["ExternalComfyAdapter", "ImageAdapter"]
