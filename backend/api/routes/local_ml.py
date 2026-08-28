"""Local-ML scaffold routes: status, one-at-a-time model download, the
per-feature enable toggle, and — for the prose rewriter — the variant selector,
the llama-server runtime fetch and model deletion.

Drives the Settings "Local ML" card, which is a tri-state per single-file
feature and an expanded panel for a feature that reports ``variants``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, HTTPException

from ...database import get_settings, set_local_ml_config, set_local_ml_enabled
from ...inference import local_ml, prose_rewriter
from ...inference.local_models import assets, catalog, dependencies
from ...inference.prose_rewriter import runtime as llama_runtime

logger = logging.getLogger(__name__)

router = APIRouter()

_download_lock = asyncio.Lock()


def _require(feature: str) -> catalog.ModelSpec:
    if feature not in catalog.MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown local-ML feature {feature!r}")
    return catalog.MODELS[feature]


def _prose_config(settings) -> dict:
    return (settings.get("local_ml_config") or {}).get(prose_rewriter.FEATURE) or {}


#: Strong references to fire-and-forget pre-warm tasks. Without this the only
#: reference is the event loop's weak one and the task can be collected
#: mid-load, which shows up as a model that silently never finishes warming.
_BACKGROUND: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def _sync_selection(feature: str, spec: catalog.ModelSpec, *, prefer: str | None = None) -> dict:
    """Keep the stored variant pointing at a checkpoint that is actually on disk.

    Downloading a GGUF did not select it and deleting one did not deselect it,
    so both ends of the obvious workflow landed in the same silent state: the
    feature enabled, ``resolve_prose_rewrite`` returning ``None`` because the
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
    if feature != prose_rewriter.FEATURE or not spec.variants:
        return settings.get("local_ml_config", {})
    config = _prose_config(settings)
    current = prose_rewriter.resolve(str(config.get("variant") or ""))
    if current is not None and prose_rewriter.on_disk(current):
        return settings.get("local_ml_config", {})
    present = [v for v in spec.variants if prose_rewriter.on_disk(v)]
    picked = next((v for v in present if v.id == prefer), None) or (present[0] if present else None)
    gpu = bool(config.get("gpu", True))
    batch_size = prose_rewriter.resolve_batch_size(config.get("batch_size"))
    await set_local_ml_config(feature, {"variant": picked.id if picked else None, "gpu": gpu, "batch_size": batch_size})
    # Same follow-through as the config route: the selection moved, so a loaded
    # child is stale, and the next turn should not pay for the load.
    prose_rewriter.HOST.mark_stale(picked, gpu, batch_size)
    if picked is not None and prose_rewriter.available(picked.id):
        _spawn(_prewarm(picked, gpu, batch_size))
    settings = await get_settings()
    return settings.get("local_ml_config", {})


async def _prewarm(variant, gpu: bool, batch_size: int) -> None:
    """Load the model in the background so the first turn does not pay for it.

    Failures are logged, not raised: the panel reads ``HOST.state``, and a
    pre-warm that could not start is the same information as a ``failed``
    state — surfacing it as a 500 on a settings write would be worse.
    """
    try:
        await prose_rewriter.HOST.ensure(variant, gpu, batch_size)
    except Exception:
        logger.warning("Prose rewriter pre-warm failed", exc_info=True)


@router.get("/api/local-ml/status")
async def api_local_ml_status():
    """Per-feature tri-state: extras installed? model present? feature enabled?

    ``deps_ok`` is now per feature — the prose rewriter drives a child process
    and needs only ``huggingface_hub``, while the in-process classifiers need
    the ``llama-cpp-python`` binding too, so one global answer would gray out a
    button that works. The top-level ``deps_ok`` stays as the whole-extras
    answer the grouped opt-in card is keyed on.
    """
    settings = await get_settings()
    enabled_map = settings.get("local_ml_enabled", {})
    features: dict[str, dict] = {}
    for f, spec in catalog.MODELS.items():
        f_ok, f_reason = dependencies.deps_ok(f)
        info: dict = {
            "present": assets.present(f),
            "enabled": enabled_map.get(f, True),
            "size_mb": spec.size_mb,
            "deps_ok": f_ok,
            "reason": f_reason,
            "runtime": spec.runtime,
        }
        if spec.variants:
            config = _prose_config(settings) if f == prose_rewriter.FEATURE else {}
            info["variants"] = [
                {
                    "id": v.id,
                    "label": v.label,
                    "detail": v.detail,
                    "size_mb": v.size_mb,
                    "present": prose_rewriter.on_disk(v),
                }
                for v in spec.variants
            ]
            info["selected"] = config.get("variant") or None
            info["gpu"] = bool(config.get("gpu", True))
            info["batch_size"] = prose_rewriter.resolve_batch_size(config.get("batch_size"))
            info["runtime_ok"] = prose_rewriter.runtime_ok()
            info.update(prose_rewriter.state())
        features[f] = info
    deps_ok, reason = dependencies.deps_ok()
    return {
        "deps_ok": deps_ok,
        "reason": reason,
        "install_cmd": dependencies.install_cmd(),
        "features": features,
    }


@router.post("/api/local-ml/{feature}/download")
async def api_local_ml_download(feature: str, data: dict | None = Body(default=None)):  # noqa: B008
    """Download a GGUF into backend/data/models/ (one at a time).

    An optional ``{"variant": "..."}`` names one of a variant-bearing feature's
    checkpoints; without it the feature's own default file is fetched.
    """
    spec = _require(feature)
    variant = str((data or {}).get("variant") or "") or None
    # Validated here rather than inside download(): a bad id is the caller's
    # mistake and should not first take the global download lock and occupy a
    # worker thread to find that out. It is also checked *before* deps, for the
    # same reason `_require` is: whether a variant exists is a fact about the
    # request, not about the machine, so the answer must not change from 404 to
    # 400 just because this install happens to be missing the extras.
    if variant and variant not in {v.id for v in spec.variants}:
        raise HTTPException(status_code=404, detail=f"Unknown variant {variant!r} for {feature!r}")
    ok, reason = dependencies.deps_ok(feature)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    async with _download_lock:
        try:
            await asyncio.to_thread(assets.download, feature, variant)
        except Exception:
            logger.exception("local-ml download %r (%s) failed", feature, variant)
            raise HTTPException(status_code=500, detail="Download failed; see server logs") from None
    config = await _sync_selection(feature, spec, prefer=variant)
    return {"ok": True, "present": assets.present(feature), "local_ml_config": config}


@router.delete("/api/local-ml/{feature}/model")
async def api_local_ml_delete_model(feature: str, variant: str | None = None):
    """Delete one downloaded GGUF.

    Exists because the three prose-rewriter checkpoints are 9.6 GB combined and
    "go find the folder" is not an acceptable only exit at that size.
    """
    spec = _require(feature)
    if variant and variant not in {v.id for v in spec.variants}:
        raise HTTPException(status_code=404, detail=f"Unknown variant {variant!r} for {feature!r}")
    if feature == prose_rewriter.FEATURE:
        # BEFORE the unlink, not after: llama.cpp mmaps the GGUF, and Windows
        # refuses to delete a mapped file — the request would 500 and the only
        # way out would be waiting out the idle unload or restarting Orb.
        # `release` drains first, so a rewrite in flight finishes rather than
        # being cut off, and the next use reloads whatever is still on disk.
        await prose_rewriter.HOST.release()
    try:
        removed = await asyncio.to_thread(assets.delete_model, feature, variant)
    except OSError:
        logger.exception("local-ml delete %r (%s) failed", feature, variant)
        raise HTTPException(status_code=500, detail="Delete failed; see server logs") from None
    # After the unlink, so the sweep reads the disk as it now is: deleting the
    # selected checkpoint hands the selection to another one that is present.
    config = await _sync_selection(feature, spec)
    return {"ok": True, "removed": removed, "present": assets.present(feature), "local_ml_config": config}


@router.post("/api/local-ml/{feature}/config")
async def api_local_ml_config(feature: str, data: dict = Body(...)):  # noqa: B008
    """Set one feature's config (prose rewriter: variant, GPU and batch size).

    Any change marks the host stale and RETURNS IMMEDIATELY. Draining and
    restarting inline would block a settings write on a turn that may be
    mid-rewrite, or kill it. The background pre-warm finishes the current
    rewrite, then reloads with the new allocation; new work waits behind it.

    Then it pre-warms in the background. Loading 2.2-4.7 GB from cold is
    seconds to tens of seconds, and paying that inside the first turn after
    flipping the toggle looks like a hang; kicking it off here means the model
    is hot while the user is still in Settings.
    """
    spec = _require(feature)
    if not spec.variants:
        raise HTTPException(status_code=404, detail=f"{feature!r} has no configurable variants")
    variant_id = str(data.get("variant") or "") or None
    if variant_id and variant_id not in {v.id for v in spec.variants}:
        raise HTTPException(status_code=404, detail=f"Unknown variant {variant_id!r} for {feature!r}")
    gpu = bool(data.get("gpu", True))
    raw_batch_size = data.get("batch_size", prose_rewriter.DEFAULT_BATCH_SIZE)
    batch_size = prose_rewriter.select_batch_size(raw_batch_size)
    if batch_size is None:
        raise HTTPException(
            status_code=400,
            detail=(f"batch_size must be an integer from {prose_rewriter.MIN_BATCH_SIZE} to {prose_rewriter.MAX_BATCH_SIZE}"),
        )
    await set_local_ml_config(feature, {"variant": variant_id, "gpu": gpu, "batch_size": batch_size})
    settings = await get_settings()
    if feature == prose_rewriter.FEATURE:
        variant = prose_rewriter.resolve(variant_id)
        prose_rewriter.HOST.mark_stale(variant, gpu, batch_size)
        if variant is not None and prose_rewriter.on_disk(variant):
            _spawn(_prewarm(variant, gpu, batch_size))
    return {"local_ml_config": settings.get("local_ml_config", {})}


@router.post("/api/local-ml/prose_rewriter/runtime")
async def api_prose_rewriter_runtime(data: dict | None = Body(default=None)):  # noqa: B008
    """Fetch a prebuilt llama-server into backend/data/llama-bin/.

    ``{"backend": "gpu"|"cpu"}`` picks the archive — Vulkan or plain CPU — and
    that choice is baked into the binary, not into a runtime flag. This
    downloads and then executes a native binary from the official ggml-org
    release feed; ``ORB_LLAMA_SERVER`` is the escape hatch for a self-supplied
    one.
    """
    backend = "cpu" if str((data or {}).get("backend") or "gpu") == "cpu" else "gpu"
    # A re-fetch replaces backend/data/llama-bin/ wholesale, and on Windows a
    # running executable cannot be unlinked. Stop the child first — it reloads
    # on next use, against the binary that just landed rather than the one it
    # was started from, which is what someone switching CPU↔Vulkan is asking for.
    await prose_rewriter.HOST.release()
    async with _download_lock:
        try:
            path = await asyncio.to_thread(llama_runtime.fetch, backend)
        except llama_runtime.LlamaServerMissing as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None
        except Exception:
            logger.exception("llama-server fetch (%s) failed", backend)
            raise HTTPException(status_code=500, detail="Runtime download failed; see server logs") from None
    return {"ok": True, "path": path}


@router.post("/api/local-ml/slop-score")
async def api_slop_score(data: dict = Body(...)):  # noqa: B008
    """Score each sentence for AI-slop → {"scores": [float in 0..1, ...]} in input order.

    Sentences come pre-split from the frontend (which owns the coloring), so scores
    map back to spans by index. 503 when the extra/model is missing or the toggle is off.
    """
    ok, reason = local_ml.available("slop_classifier")
    settings = await get_settings()
    if not ok or not settings.get("local_ml_enabled", {}).get("slop_classifier", True):
        raise HTTPException(status_code=503, detail=reason or "AI-Slop Classifier disabled")
    sentences = [str(s) for s in (data.get("sentences") or [])][:400]  # cap runaway input
    scores = await local_ml.ascore("slop_classifier", sentences)
    return {"scores": scores}


@router.post("/api/local-ml/classify-emotion")
async def api_classify_emotion(data: dict = Body(...)):  # noqa: B008
    """Classify one text → {"label": go-emotions label}.

    The frontend sends only the last few sentences of the latest assistant message
    (recency is enforced caller-side; the model isn't trusted to weight late text).
    503 when the extra/model is missing or the toggle is off — the expression popup
    treats that as "no expressions" and falls back to the plain avatar.
    """
    ok, reason = local_ml.available("emotion_classifier")
    settings = await get_settings()
    if not ok or not settings.get("local_ml_enabled", {}).get("emotion_classifier", True):
        raise HTTPException(status_code=503, detail=reason or "Character Expressions disabled")
    label = await local_ml.aclassify("emotion_classifier", str(data.get("text") or ""))
    return {"label": label}


@router.post("/api/local-ml/{feature}/enabled")
async def api_local_ml_enabled(feature: str, data: dict = Body(...)):  # noqa: B008
    """Flip one feature's on/off toggle; return the full decoded map."""
    spec = _require(feature)
    enabled = bool(data.get("enabled"))
    await set_local_ml_enabled(feature, enabled)
    # Switching a feature on means "make this work", so it is also the moment to
    # repair a selection that points at nothing — the state an install that
    # downloaded a checkpoint before this sweep existed is sitting in, and the
    # one place such an install reliably passes through. When it does repair
    # one it also pre-warms it, and the block below then asks for the same
    # variant a second time: `ensure` short-circuits on a healthy host, so the
    # duplicate is a no-op rather than a second load.
    config_blob = await _sync_selection(feature, spec) if enabled else {}
    settings = await get_settings()
    if enabled and feature == prose_rewriter.FEATURE:
        # Pre-warm on enable, for the same reason the config route does.
        config = _prose_config(settings)
        variant = prose_rewriter.resolve(str(config.get("variant") or ""))
        if variant is not None and prose_rewriter.on_disk(variant):
            _spawn(
                _prewarm(
                    variant,
                    bool(config.get("gpu", True)),
                    prose_rewriter.resolve_batch_size(config.get("batch_size")),
                )
            )
    return {
        "local_ml_enabled": settings.get("local_ml_enabled", {}),
        "local_ml_config": config_blob or settings.get("local_ml_config", {}),
    }
