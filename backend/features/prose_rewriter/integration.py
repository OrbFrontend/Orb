"""The database-aware management surface: selection repair, pre-warm, fetch.

Separate from :mod:`service` on purpose. A rewrite call needs no database and
``rewrite_events`` must stay importable without one; everything here reads or
writes ``settings``, and folding the two together is how a slice grows a god
module.

The API layer composes these — it validates a feature id, maps an error to a
status code, and serialises the result. What is *decided* here is behaviour:
which checkpoint the selection should point at, when to pre-warm, and what has
to let go of a file before it can be replaced.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ...database import get_settings, set_local_ml_config
from ...inference.local_models import llama_server
from ...inference.local_models.llama_server import LaunchProfile
from . import catalog, config
from .service import HOST, state

logger = logging.getLogger(__name__)

#: Strong references to fire-and-forget pre-warm tasks. Without this the only
#: reference is the event loop's weak one and the task can be collected
#: mid-load, which shows up as a model that silently never finishes warming.
_BACKGROUND: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def _prewarm(profile: LaunchProfile) -> None:
    """Load the model in the background so the first turn does not pay for it.

    Failures are logged, not raised: the panel reads ``HOST.state``, and a
    pre-warm that could not start is the same information as a ``failed``
    state — surfacing it as a 500 on a settings write would be worse.
    """
    try:
        await HOST.ensure(profile)
    except Exception:
        logger.warning("Prose rewriter pre-warm failed", exc_info=True)


def _stored(settings: Mapping[str, Any]) -> dict:
    return (settings.get("local_ml_config") or {}).get(catalog.FEATURE) or {}


def _apply(profile: LaunchProfile | None) -> None:
    """Record a new selection and warm it, without blocking on the old one.

    Marking stale RETURNS IMMEDIATELY: draining and restarting inline would
    block a settings write on a turn that may be mid-rewrite, or kill it. The
    background pre-warm finishes the current rewrite, then reloads with the new
    allocation; new work waits behind it. Loading 2.2-4.7 GB from cold is
    seconds to tens of seconds, and paying that inside the first turn after
    flipping the toggle looks like a hang.
    """
    HOST.mark_stale(profile)
    if profile is not None:
        _spawn(_prewarm(profile))


async def status_extra(settings: Mapping[str, Any]) -> dict:
    """The panel fields only this feature can answer."""
    stored = _stored(settings)
    return {
        "selected": stored.get("variant") or None,
        "gpu": bool(stored.get("gpu", True)),
        "batch_size": config.resolve_batch_size(stored.get("batch_size")),
        **state(),
    }


async def sync_selection(*, prefer: str | None = None) -> dict:
    """Keep the stored variant pointing at a checkpoint that is actually on disk.

    Downloading a GGUF did not select it and deleting one did not deselect it,
    so both ends of the obvious workflow landed in the same silent state: the
    feature enabled, ``resolve_config`` returning ``None`` because the
    selection names nothing, and every turn skipping the rewriter with no cue
    in the card or the log. The radio was the only thing that armed it, and
    nothing said so.

    This only ever *fills a hole* — a live pick whose file is on disk is user
    data and is never overridden, which is what keeps ``catalog``'s rule intact
    (there is still no implicit default; the choice is written down, and the
    radio the user is looking at agrees with it). *prefer* is the file that
    just arrived, and it wins only among candidates when the current pick has
    nothing behind it.

    Returns the whole ``local_ml_config`` blob so the caller can hand the
    client its new copy in the same response.
    """
    settings = await get_settings()
    stored = _stored(settings)
    current = catalog.resolve(str(stored.get("variant") or ""))
    if current is not None and catalog.on_disk(current):
        return settings.get("local_ml_config", {})
    present = [v for v in catalog.variants() if catalog.on_disk(v)]
    picked = next((v for v in present if v.id == prefer), None) or (present[0] if present else None)
    gpu = bool(stored.get("gpu", True))
    batch_size = config.resolve_batch_size(stored.get("batch_size"))
    await set_local_ml_config(catalog.FEATURE, {"variant": picked.id if picked else None, "gpu": gpu, "batch_size": batch_size})
    # Same follow-through as the config route: the selection moved, so a loaded
    # child is stale, and the next turn should not pay for the load.
    _apply(config.profile_for_selection(picked, gpu, batch_size))
    settings = await get_settings()
    return settings.get("local_ml_config", {})


async def apply_config(body: Mapping[str, Any]) -> dict:
    """Persist variant/GPU/batch size from a request body; return the new blob.

    Validates into the feature's own error types. The route maps them to status
    codes — a slice does not import FastAPI to say 404.
    """
    variant_id = str(body.get("variant") or "") or None
    if variant_id and catalog.resolve(variant_id) is None:
        raise config.UnknownVariant(f"Unknown variant {variant_id!r} for {catalog.FEATURE!r}")
    gpu = bool(body.get("gpu", True))
    batch_size = config.select_batch_size(body.get("batch_size", config.DEFAULT_BATCH_SIZE))
    if batch_size is None:
        raise config.UnsupportedBatchSize(
            f"batch_size must be an integer from {config.MIN_BATCH_SIZE} to {config.MAX_BATCH_SIZE}"
        )
    await set_local_ml_config(catalog.FEATURE, {"variant": variant_id, "gpu": gpu, "batch_size": batch_size})
    _apply(config.profile_for_selection(catalog.resolve(variant_id), gpu, batch_size))
    settings = await get_settings()
    return settings.get("local_ml_config", {})


async def on_enabled(enabled: bool) -> None:
    """Switching the feature on means "make this work".

    So it is also the moment to repair a selection that points at nothing — the
    state an install that downloaded a checkpoint before the sweep existed is
    sitting in, and the one place such an install reliably passes through. The
    repair pre-warms what it picks, and the pre-warm below then asks for the
    same variant a second time: ``ensure`` short-circuits on a healthy host, so
    the duplicate is a no-op rather than a second load.
    """
    if not enabled:
        return
    await sync_selection()
    settings = await get_settings()
    stored = _stored(settings)
    _apply(
        config.profile_for_selection(
            catalog.resolve(str(stored.get("variant") or "")),
            bool(stored.get("gpu", True)),
            config.resolve_batch_size(stored.get("batch_size")),
        )
    )


async def release_host() -> None:
    """Let go of the GGUF before something unlinks it.

    BEFORE the unlink, not after: llama.cpp mmaps the weights, and Windows
    refuses to delete a mapped file — the request would 500 and the only way
    out would be waiting out the idle unload or restarting Orb. ``release``
    drains first, so a rewrite in flight finishes rather than being cut off,
    and the next use reloads whatever is still on disk.
    """
    await HOST.release()


async def fetch_runtime(backend: str) -> str:
    """Download and unpack a llama-server, after every host lets go of the old one.

    A re-fetch replaces ``backend/data/llama-bin/`` wholesale, and on Windows a
    running executable cannot be unlinked. Every registered host is released
    first — not just this feature's — because the binary is shared: each
    reloads on next use against the binary that just landed rather than the one
    it was started from, which is what someone switching CPU↔Vulkan is asking
    for. Blocking; run it in a thread.
    """
    await llama_server.manager.release_all()
    return await asyncio.to_thread(llama_server.binary.fetch, backend)
