"""Adapter dispatch for one render."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import ImageRequest, ImageResult, ProgressCallback
from .router import get_adapter
from .target import RenderTarget


async def resolve_and_generate(
    config: Mapping[str, Any],
    request: ImageRequest,
    *,
    target: RenderTarget,
    progress: ProgressCallback | None = None,
) -> ImageResult:
    """Render `request` on the target the caller already resolved.

    `target` is required, and there is deliberately no `replay=`: both paths in
    `hooks.py` need answers off the target before the call, and a second way to
    reach one is how they come to disagree about replay precedence.
    """
    return await get_adapter(config).generate(request, target=target, progress=progress)
