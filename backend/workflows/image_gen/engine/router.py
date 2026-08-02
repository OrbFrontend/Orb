"""Image-backend adapter registry and routing.

Maps the connection *a style names* to an adapter class. Adapters are registered
only if their dependencies import, mirroring ``tts/engine/router.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..config import style_source
from .adapters.base import ImageAdapter

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type[ImageAdapter]] = {}
# Also held by name: `node_types` needs the ComfyUI adapter with its ComfyUI-only
# `node_roles` still visible on the type.
_COMFY: type[ExternalComfyAdapter] | None = None

# ── Register adapters (graceful — skip if a dependency is missing) ────────────

try:
    from .adapters.external_comfy import ExternalComfyAdapter

    _COMFY = ExternalComfyAdapter
    _REGISTRY[ExternalComfyAdapter.source_id] = ExternalComfyAdapter
except ImportError:  # pragma: no cover — httpx is a hard dependency today
    logger.info("httpx not installed — external ComfyUI image backend disabled")

try:
    from .adapters.openai_image import OpenAICompatibleImageAdapter

    _REGISTRY[OpenAICompatibleImageAdapter.source_id] = OpenAICompatibleImageAdapter
except ImportError:  # pragma: no cover — httpx is a hard dependency today
    logger.info("httpx not installed — cloud API image backend disabled")

# What an unknown or unavailable source falls back to. Never raising is deliberate:
# `source` arrives from a stored config, and a hand-edited DB should not turn every
# page load into a 500.
_FALLBACK = "external_comfy"


def get_adapter(config: Mapping[str, Any], style: Mapping[str, Any]) -> ImageAdapter:
    """The adapter that renders `style`, bound to both.

    `style` is required and positional, so no render path can quietly fall back to
    the default style. Routing on `config["source"]` -- which `normalize_config`
    derives from the *default* style -- was wrong for every path that names another:
    a rehydrate replays the style the stored image recorded, so a ComfyUI-linked
    style rehydrated while the default style is cloud-linked went to the cloud
    adapter. It survived only because that adapter ignored the style it was handed.
    """
    source, _ = style_source(config, style)
    cls = _REGISTRY.get(source) or _REGISTRY.get(_FALLBACK)
    if cls is None:  # pragma: no cover — the fallback adapter has no optional deps
        raise RuntimeError("No image generation backend is available")
    if source not in _REGISTRY:
        logger.warning("unknown image source %r; falling back to %s", source, cls.source_id)
    return cls(config, style)


def comfy_adapter(config: Mapping[str, Any]) -> ExternalComfyAdapter:
    """The ComfyUI adapter explicitly, whatever any style links to. Graphs are global
    and the importer stays usable under cloud, so `node_types` must never route by
    a style's connection.

    No style argument: its one caller asks `node_roles()`, which is pure network and
    has no render target to answer about.
    """
    if _COMFY is None:  # pragma: no cover — the ComfyUI adapter has no optional deps
        raise RuntimeError("The external ComfyUI backend is unavailable")
    return _COMFY(config)


def list_sources() -> list[dict]:
    """Every registered backend, for the source picker. Reads ClassVars rather than
    instantiating: image adapters bind a config at construction, and ``status`` has
    no business building clients to answer a menu."""
    return [{"id": name, "label": cls.display_name, "capabilities": dict(cls.capabilities)} for name, cls in _REGISTRY.items()]
