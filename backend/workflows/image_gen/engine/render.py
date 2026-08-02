"""Style/replay resolution and adapter dispatch."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..config import resolve_style
from .contracts import ImageRequest, ImageResult, ProgressCallback
from .router import get_adapter
from .target import RenderTarget


def resolve_render_target(
    config: Mapping[str, Any],
    style_id: str,
    replay: Mapping[str, Any] | None = None,
) -> RenderTarget:
    """What will execute a fresh render of `style_id`, or a replay of a stored one."""
    return get_adapter(config).resolve_target(resolve_style(config, style_id), replay)


async def resolve_and_generate(
    config: Mapping[str, Any],
    request: ImageRequest,
    *,
    target: RenderTarget | None = None,
    progress: ProgressCallback | None = None,
) -> ImageResult:
    """Render, resolving the target from the style only when the caller has none.

    Both render paths in `hooks.py` resolve first and pass `target=`, because both
    need answers off it before the call: the fresh path needs the negative-prompt
    and reference slots to compose, the reroll path needs the seed and reference
    answers to decide what to disclose. There is deliberately no `replay=` here --
    a second way to reach the same target is how the two paths come to disagree
    about replay precedence.
    """
    adapter = get_adapter(config)
    if target is None:
        target = adapter.resolve_target(resolve_style(config, request.style_id), None)
    return await adapter.generate(request, target=target, progress=progress)
