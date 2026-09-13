"""Host integration for the Prose Rewriter workflow and its Local ML runtime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ..database import get_settings, set_local_ml_config
from ..inference.local_models import llama_server
from ..inference.local_models.llama_server import LaunchProfile
from ..inference.local_models.prose_rewriter import catalog, config
from ..inference.local_models.prose_rewriter.config import (
    ProseRewriteConfig,
    UnknownVariant,
    UnsupportedBatchSize,
)
from ..inference.local_models.prose_rewriter.service import HOST, rewrite_events, state
from .contracts import EV_DRAFT_REPLACED

logger = logging.getLogger(__name__)

FEATURE = catalog.FEATURE

# Strong references to fire-and-forget pre-warm tasks. Without this the only
# reference is the event loop's weak one and the task can be collected
# mid-load, which shows up as a model that silently never finishes warming.
_BACKGROUND: set[asyncio.Task] = set()


def resolve_config(settings: Mapping[str, Any]) -> ProseRewriteConfig | None:
    """Resolve the Local ML selection used by automatic and manual rewrites.

    Workflow enablement is intentionally not read here. The workflow bridge
    applies it to automatic post-pipeline execution, while the dedicated saved-
    message action remains available whenever the local engine itself is on.
    """
    raw_enabled = settings.get("local_ml_enabled")
    if isinstance(raw_enabled, Mapping) and raw_enabled.get(FEATURE, True) is False:
        return None
    raw_configs = settings.get("local_ml_config")
    raw_selection = raw_configs.get(FEATURE) if isinstance(raw_configs, Mapping) else None
    selection = raw_selection if isinstance(raw_selection, Mapping) else {}
    variant_id = str(selection.get("variant") or "")
    if not config.runnable(variant_id):
        return None
    return {
        "variant_id": variant_id,
        "gpu": bool(selection.get("gpu", True)),
        "batch_size": config.resolve_batch_size(selection.get("batch_size")),
    }


async def post_pipeline(ctx):
    """Rewrite the post-Editor draft before later secondary workflows."""
    cfg = resolve_config(ctx.settings)
    draft = ctx.draft
    if cfg is None or not draft:
        return
    logger.info(
        "Prose rewriter starting after Editor (draft=%d chars, variant=%s)",
        len(draft),
        cfg["variant_id"],
    )
    async for event in rewrite_events(draft, cfg):
        if event["type"] == "draft_update":
            yield {"event": "draft_update", "data": {"draft": event["draft"]}}
        elif event["type"] == "warning":
            yield {
                "event": "warning",
                "data": {
                    "headline": "Prose rewriter didn't run",
                    "sentence": event["reason"],
                    "kind": "local_ml",
                },
            }
        elif event["type"] == "rewritten" and event["draft"] != draft:
            yield {"type": EV_DRAFT_REPLACED, "draft": event["draft"]}


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def _prewarm(profile: LaunchProfile) -> None:
    """Load the model in the background so the first rewrite does not pay for it."""
    try:
        await HOST.ensure(profile)
    except Exception:
        logger.warning("Prose rewriter pre-warm failed", exc_info=True)


def _stored(settings: Mapping[str, Any]) -> dict:
    configs = settings.get("local_ml_config")
    stored = configs.get(FEATURE) if isinstance(configs, Mapping) else None
    return dict(stored) if isinstance(stored, Mapping) else {}


def _apply(profile: LaunchProfile | None) -> None:
    """Record a new selection and warm it without interrupting an active rewrite."""
    HOST.mark_stale(profile)
    if profile is not None:
        _spawn(_prewarm(profile))


async def status_extra(settings: Mapping[str, Any]) -> dict:
    """Return Local ML status fields specific to this workflow's engine."""
    stored = _stored(settings)
    return {
        "selected": stored.get("variant") or None,
        "gpu": bool(stored.get("gpu", True)),
        "batch_size": config.resolve_batch_size(stored.get("batch_size")),
        **state(),
    }


async def sync_selection(*, prefer: str | None = None) -> dict:
    """Keep the stored variant pointed at an installed checkpoint."""
    settings = await get_settings()
    stored = _stored(settings)
    current = catalog.resolve(str(stored.get("variant") or ""))
    if current is not None and catalog.on_disk(current):
        return settings.get("local_ml_config", {})
    present = [variant for variant in catalog.variants() if catalog.on_disk(variant)]
    picked = next((variant for variant in present if variant.id == prefer), None) or (present[0] if present else None)
    gpu = bool(stored.get("gpu", True))
    batch_size = config.resolve_batch_size(stored.get("batch_size"))
    await set_local_ml_config(
        FEATURE,
        {"variant": picked.id if picked else None, "gpu": gpu, "batch_size": batch_size},
    )
    _apply(config.profile_for_selection(picked, gpu, batch_size))
    settings = await get_settings()
    return settings.get("local_ml_config", {})


async def apply_config(body: Mapping[str, Any]) -> dict:
    """Persist variant, GPU, and batch-size settings and return the config blob."""
    variant_id = str(body.get("variant") or "") or None
    if variant_id and catalog.resolve(variant_id) is None:
        raise UnknownVariant(f"Unknown variant {variant_id!r} for {FEATURE!r}")
    gpu = bool(body.get("gpu", True))
    batch_size = config.select_batch_size(body.get("batch_size", config.DEFAULT_BATCH_SIZE))
    if batch_size is None:
        supported = ", ".join(str(size) for size in config.SLOT_ALLOCATION)
        raise UnsupportedBatchSize(f"batch_size must be one of {supported}")
    await set_local_ml_config(
        FEATURE,
        {"variant": variant_id, "gpu": gpu, "batch_size": batch_size},
    )
    _apply(config.profile_for_selection(catalog.resolve(variant_id), gpu, batch_size))
    settings = await get_settings()
    return settings.get("local_ml_config", {})


async def _stored_profile(settings: Mapping[str, Any]) -> LaunchProfile | None:
    stored = _stored(settings)
    return config.profile_for_selection(
        catalog.resolve(str(stored.get("variant") or "")),
        bool(stored.get("gpu", True)),
        config.resolve_batch_size(stored.get("batch_size")),
    )


async def on_enabled(enabled: bool) -> None:
    """Warm the engine when enabled and release its memory when disabled."""
    if not enabled:
        await HOST.release()
        return
    await sync_selection()
    _apply(await _stored_profile(await get_settings()))


async def release_host() -> None:
    """Release the mmap and child process before deleting a model file."""
    await HOST.release()


async def fetch_runtime() -> str:
    """Install the shared llama-server builds and pre-warm the selected model."""
    await llama_server.manager.release_all()
    path = await asyncio.to_thread(llama_server.binary.fetch)
    settings = await get_settings()
    enabled = settings.get("local_ml_enabled")
    if not isinstance(enabled, Mapping) or enabled.get(FEATURE, True):
        _apply(await _stored_profile(settings))
    return path


__all__ = [
    "FEATURE",
    "ProseRewriteConfig",
    "UnknownVariant",
    "UnsupportedBatchSize",
    "apply_config",
    "fetch_runtime",
    "on_enabled",
    "post_pipeline",
    "release_host",
    "resolve_config",
    "rewrite_events",
    "status_extra",
    "sync_selection",
]
