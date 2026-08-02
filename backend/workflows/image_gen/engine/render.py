"""The render seam.

One named function every render goes through, so a test can replace "what the
backend did" without standing up a transport. Deliberately takes the adapter the
caller already resolved rather than a config: `hooks.py` needs `resolve_target`
and `label` off that adapter before this call, and looking one up again here made
"which backend renders" a decision taken twice from the same config.
"""

from __future__ import annotations

from .adapters.base import ImageAdapter
from .contracts import ImageRequest, ImageResult, ProgressCallback, RenderTarget


async def resolve_and_generate(
    adapter: ImageAdapter,
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
    return await adapter.generate(request, target=target, progress=progress)
