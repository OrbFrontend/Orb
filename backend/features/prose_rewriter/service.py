"""Run the prose rewriter and expose its event stream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator

from ...inference.local_models.llama_server import ManagedLlamaServerHost
from . import config
from .rewrite import arewrite

logger = logging.getLogger(__name__)

#: One host per process. The rewriter is a single-user local feature and a
#: second resident model would double the VRAM for no gain. Constructing it
#: registers it with the shared runtime manager, which is what lets the app
#: lifespan stop a child it knows nothing else about.
HOST = ManagedLlamaServerHost(name="prose_rewriter", idle_timeout=config.IDLE_TIMEOUT)

#: Queue sentinel: the rewrite task has finished (or failed) and no more
#: snapshots are coming. A distinct object so a snapshot can never spell it.
_DONE = object()


def available(variant_id: str | None) -> bool:
    """Is there a selected variant, on disk, *and* a runtime binary?"""
    return config.runnable(variant_id)


def state() -> dict[str, str]:
    """``{"state": idle|loading|ready|failed, "error": …}`` for the panel."""
    return {"state": HOST.state, "error": HOST.error}


async def shutdown() -> None:
    """Stop this feature's llama-server child.

    The app lifespan stops every host through the shared manager; this is the
    one-host spelling, kept for symmetry with :func:`state` and for a caller
    that means this feature specifically.
    """
    await HOST.shutdown()


async def rewrite_events(draft: str, cfg: config.ProseRewriteConfig) -> AsyncGenerator[dict, None]:
    """Rewrite *draft*, yielding the caller-independent event vocabulary.

    Yields:
        ``{"type": "draft_update", "draft": str}`` — one per completed
        top-to-bottom run of paragraphs, carrying the WHOLE current assembly
        rather than a delta; generation is concurrent, so there is no
        meaningful delta.
        ``{"type": "warning", "reason": str}`` — the rewrite did not happen.
        ``{"type": "rewritten", "draft": str}`` — exactly once, last. Terminal
        and internal: a caller consumes it and never forwards it as-is.
    """
    # A queue bridges the rewriter's progress callback into this generator: an
    # async generator cannot yield from inside a callback its own body is
    # awaiting, and batching the repaints until the end would leave the bubble
    # frozen for the whole rewrite — which is the hang this event exists to
    # avoid. The rewrite runs as a task; this loop drains snapshots as they land.
    queue: asyncio.Queue[str | object] = asyncio.Queue()

    async def worker() -> str:
        try:
            # The profile is built HERE, inside the task: a checkpoint that
            # went missing, a registry change since the config was resolved, or
            # a batch size outside the allowlist all raise, and inside the task
            # that failure reaches the `except` below as one warning plus the
            # writer's draft, which is this generator's whole contract.
            profile = config.launch_profile(cfg)
            return await arewrite(draft, profile, host=HOST, on_progress=queue.put)
        finally:
            queue.put_nowait(_DONE)

    task = asyncio.create_task(worker())
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            yield {"type": "draft_update", "draft": str(item)}
        rewritten = await task
    except Exception as exc:
        logger.warning("Prose rewriter failed; keeping the writer's draft", exc_info=True)
        yield {"type": "warning", "reason": str(exc) or exc.__class__.__name__}
        yield {"type": "rewritten", "draft": draft}
        return
    finally:
        # Abandoning this generator (an abort mid-rewrite) must not leave the
        # task decoding into a queue nobody reads. Cancelling closes the
        # connection, which is llama.cpp's cancel signal, so the slots free.
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    yield {"type": "rewritten", "draft": rewritten}
