"""Render an image request against an already resolved target."""

from __future__ import annotations

from dataclasses import replace

from .adapters.base import ImageAdapter
from .contracts import (
    ImageRequest,
    ImageResult,
    ProgressCallback,
    RenderTarget,
    fold_seed_into,
)


async def resolve_and_generate(
    adapter: ImageAdapter,
    request: ImageRequest,
    *,
    target: RenderTarget,
    progress: ProgressCallback | None = None,
) -> ImageResult:
    """Apply explicit compatibility settings and make exactly one render attempt.

    Provider messages are deliberately not interpreted here. If the request is
    rejected, the remote message reaches the user and they decide what to change.
    """
    if target.supports_seed and target.seed_max is not None:
        request = replace(request, seed=fold_seed_into(request.seed, 0, target.seed_max))
    return await adapter.generate(request, target=target, progress=progress)
