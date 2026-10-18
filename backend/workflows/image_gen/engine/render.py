"""Render an image request against an already resolved target."""

from __future__ import annotations

from dataclasses import replace

from ...toolkit import WorkflowUnavailableError, comfy_gpu_lease
from .adapters.base import ImageAdapter
from .contracts import ImageGenerationError, ImageRequest, ImageResult, ProgressCallback, RenderTarget, emit, fold_seed_into


async def resolve_and_generate(
    adapter: ImageAdapter, request: ImageRequest, *, target: RenderTarget, progress: ProgressCallback | None = None
) -> ImageResult:
    """Apply explicit compatibility settings and make exactly one render attempt.

    Provider messages are deliberately not interpreted here. If the request is
    rejected, the remote message reaches the user and they decide what to change.
    A ComfyUI render holds the GPU swap lease, so a local LLM is off the GPU while it runs.
    """
    if target.supports_seed and target.seed_max is not None:
        request = replace(request, seed=fold_seed_into(request.seed, 0, target.seed_max))
    if target.source != "external_comfy":
        return await adapter.generate(request, target=target, progress=progress)
    try:
        async with comfy_gpu_lease(lambda stage: emit(progress, f"gpu_{stage}", {})):
            return await adapter.generate(request, target=target, progress=progress)
    except WorkflowUnavailableError as exc:
        raise ImageGenerationError(str(exc)) from exc
