"""Image backend adapters."""

from .external_comfy import CAPABILITIES, generate, list_models, validate_connection

__all__ = ["CAPABILITIES", "generate", "list_models", "validate_connection"]
