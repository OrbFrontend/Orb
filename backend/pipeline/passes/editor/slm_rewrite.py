"""
passes/editor/slm_rewrite.py — the local prose-rewriter step.

Runs a purpose-trained SLM over the writer's draft, one paragraph at a time,
**before** the editor's audit. That ordering is the whole point: the scanners
must see the prose that will actually be persisted, and ``build_targets``
anchors byte offsets into the exact string it was handed. Rewriting *after* the
audit would leave every ``Target`` pointing into a draft that no longer exists
— "a stale list silently edits the wrong sentences" (``analysis/patching.py``).

:func:`resolve_prose_rewrite` converts raw settings into the config; a non-None
result means the feature is live this turn. Unlike ``resolve_length_guard`` it
is **not** agent-gated: the rewriter is a local model on its own Local ML
toggle and has nothing to do with whether the remote Agent passes are on.

THE SAFE FAILURE MODE IS A BYTE-IDENTICAL NO-OP, the same contract
``format_consistency`` states. No binary, no GGUF, a boot timeout, a dead
child, an HTTP error — all of them keep the writer's prose and emit one
warning. A local nicety must never cost a turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, TypedDict

from ....inference import local_ml, prose_rewriter

logger = logging.getLogger(__name__)

FEATURE = prose_rewriter.FEATURE

#: Queue sentinel: the rewrite task has finished (or failed) and no more
#: snapshots are coming. A distinct object so a snapshot can never spell it.
_DONE = object()


class ProseRewrite(TypedDict):
    """Resolved prose-rewriter config. A non-None value means enabled."""

    variant_id: str
    gpu: bool


def resolve_prose_rewrite(settings: Mapping[str, Any]) -> ProseRewrite | None:
    """Resolve the rewriter config from *settings*, or ``None`` when it can't run.

    Four things must hold, and all four are cheap: the Local ML toggle is on,
    a variant is selected, that variant's GGUF is on disk, and a llama-server
    binary resolves. Checked here rather than at the seam so a turn never pays
    a filesystem walk twice and the gating in ``editor_stage`` is one boolean.
    """
    if settings.get("local_ml_enabled", {}).get(FEATURE, True) is False:
        return None
    config = (settings.get("local_ml_config") or {}).get(FEATURE) or {}
    variant_id = str(config.get("variant") or "")
    ok, _reason = prose_rewriter.available(variant_id)
    if not ok:
        return None
    # `gpu` defaults on: someone who fetched the Vulkan build meant to use it,
    # and the checkbox is how they say otherwise.
    return {"variant_id": variant_id, "gpu": bool(config.get("gpu", True))}


async def prose_rewrite_step(draft: str, config: ProseRewrite) -> AsyncIterator[dict]:
    """Rewrite *draft*, yielding the editor pass's internal event vocabulary.

    Yields:
        ``{"type": "draft_update", "draft": str}`` — one per completed
        paragraph, carrying the WHOLE current assembly rather than a delta;
        paragraphs finish out of order, so there is no meaningful delta.
        ``{"type": "warning", "reason": str}`` — the rewrite did not happen.
        ``{"type": "rewritten", "draft": str}`` — exactly once, last. Terminal
        and internal: ``editor_pass`` consumes it and never forwards it.
    """
    variant = prose_rewriter.resolve(config["variant_id"])
    if variant is None:  # raced with a registry change between resolve and here
        yield {"type": "warning", "reason": f"Model {config['variant_id']!r} is no longer registered."}
        yield {"type": "rewritten", "draft": draft}
        return

    # A queue bridges the rewriter's progress callback into this generator: an
    # async generator cannot yield from inside a callback its own body is
    # awaiting, and batching the repaints until the end would leave the bubble
    # frozen for the whole rewrite — which is the hang this event exists to
    # avoid. The rewrite runs as a task; this loop drains snapshots as they land.
    queue: asyncio.Queue[str | object] = asyncio.Queue()

    async def worker() -> str:
        try:
            return await prose_rewriter.arewrite(draft, variant, gpu=config["gpu"], on_progress=queue.put)
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


def size_mb(variant_id: str | None) -> int:
    """Download size of *variant_id*, for the panel. 0 when unknown."""
    variant = prose_rewriter.resolve(variant_id)
    return variant.size_mb if variant is not None else local_ml.MODELS[FEATURE].size_mb
