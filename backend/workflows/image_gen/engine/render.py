"""Source/style resolution and adapter routing."""

from __future__ import annotations

from typing import Any, Mapping

from ..config import resolve_style
from .adapters import external_comfy
from .comfy_client import ProgressCallback
from .contracts import ImageRequest, ImageResult


async def resolve_and_generate(
    config: Mapping[str, Any],
    request: ImageRequest,
    *,
    progress: ProgressCallback | None = None,
) -> ImageResult:
    style = resolve_style(config, request.style_id)
    return await external_comfy.generate(
        config,
        request,
        checkpoint=style["checkpoint"],
        graph_id=style["workflow"],
        progress=progress,
    )
