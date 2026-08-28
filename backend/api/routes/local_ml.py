"""Local-ML scaffold routes: status, one-at-a-time model download, the
per-feature enable toggle, and — for a feature that has one — its own config.

Drives the Settings "Local ML" card, which is a tri-state per single-file
feature and an expanded panel for a feature that reports ``variants``.

WHAT THIS MODULE KNOWS is the shared artifact catalog: which features exist,
whether their extras are installed, whether their weights are on disk. What it
does NOT know is what any feature does with them. Feature behaviour — selection
repair, pre-warming, releasing a mapped file — is asked for by name from the
slice that owns it, and composed here.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, HTTPException

from ...database import get_settings, set_local_ml_enabled
from ...features import prose_rewriter
from ...inference import local_ml
from ...inference.local_models import assets, catalog, dependencies
from ...inference.local_models.llama_server import binary as llama_binary

logger = logging.getLogger(__name__)

router = APIRouter()

_download_lock = asyncio.Lock()


def _require(feature: str) -> catalog.ModelSpec:
    if feature not in catalog.MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown local-ML feature {feature!r}")
    return catalog.MODELS[feature]


async def _config_blob() -> dict:
    settings = await get_settings()
    return settings.get("local_ml_config", {})


async def _sync_selection(feature: str, *, prefer: str | None = None) -> dict:
    """Let a feature repair its own stored selection, and return the new blob.

    Generic here, feature behaviour there: the sweep only means something for a
    feature that has a selection to repair.
    """
    if feature == prose_rewriter.FEATURE:
        return await prose_rewriter.integration.sync_selection(prefer=prefer)
    return await _config_blob()


@router.get("/api/local-ml/status")
async def api_local_ml_status():
    """Per-feature tri-state: extras installed? model present? feature enabled?

    ``deps_ok`` is now per feature — the prose rewriter drives a child process
    and needs only ``huggingface_hub``, while the in-process classifiers need
    the ``llama-cpp-python`` binding too, so one global answer would gray out a
    button that works. The top-level ``deps_ok`` stays as the whole-extras
    answer the grouped opt-in card is keyed on.

    ``runtime_ok`` is keyed on the spec's runtime rather than on the rewriter:
    it is a fact about the shared llama-server binary, not about any feature.
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
            info["variants"] = [
                {
                    "id": v.id,
                    "label": v.label,
                    "detail": v.detail,
                    "size_mb": v.size_mb,
                    "present": assets.variant_present(v),
                }
                for v in spec.variants
            ]
        if spec.runtime == "llama_server":
            info["runtime_ok"] = llama_binary.runtime_ok()
        if f == prose_rewriter.FEATURE:
            info.update(await prose_rewriter.integration.status_extra(settings))
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
    config = await _sync_selection(feature, prefer=variant)
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
        # Before the unlink, not after — the feature explains why.
        await prose_rewriter.integration.release_host()
    try:
        removed = await asyncio.to_thread(assets.delete_model, feature, variant)
    except OSError:
        logger.exception("local-ml delete %r (%s) failed", feature, variant)
        raise HTTPException(status_code=500, detail="Delete failed; see server logs") from None
    # After the unlink, so the sweep reads the disk as it now is: deleting the
    # selected checkpoint hands the selection to another one that is present.
    config = await _sync_selection(feature)
    return {"ok": True, "removed": removed, "present": assets.present(feature), "local_ml_config": config}


@router.post("/api/local-ml/{feature}/config")
async def api_local_ml_config(feature: str, data: dict = Body(...)):  # noqa: B008
    """Set one feature's config (prose rewriter: variant, GPU and batch size).

    The body is opaque here — this route validates the feature id and hands the
    rest to the slice, which owns what its own settings mean. STATUS CODES STAY
    IN THE API and validation stays in the feature: it raises two errors of its
    own rather than importing FastAPI to say 404.
    """
    _require(feature)
    if feature != prose_rewriter.FEATURE:
        raise HTTPException(status_code=404, detail=f"{feature!r} has no configurable variants")
    try:
        config = await prose_rewriter.integration.apply_config(data)
    except prose_rewriter.UnknownVariant as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except prose_rewriter.UnsupportedBatchSize as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"local_ml_config": config}


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
    async with _download_lock:
        try:
            path = await prose_rewriter.integration.fetch_runtime(backend)
        except llama_binary.LlamaServerMissing as exc:
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
    _require(feature)
    enabled = bool(data.get("enabled"))
    await set_local_ml_enabled(feature, enabled)
    if feature == prose_rewriter.FEATURE:
        # Switching a feature on means "make this work", so the slice gets to
        # repair a selection that points at nothing, and pre-warm what it picks.
        await prose_rewriter.integration.on_enabled(enabled)
    settings = await get_settings()
    return {
        "local_ml_enabled": settings.get("local_ml_enabled", {}),
        "local_ml_config": settings.get("local_ml_config", {}),
    }
