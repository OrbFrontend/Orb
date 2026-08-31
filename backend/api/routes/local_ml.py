"""Generic local-ML status and management routes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from fastapi import APIRouter, Body, HTTPException

from ...database import get_settings, set_local_ml_enabled
from ...features import prose_rewriter
from ...inference import local_ml
from ...inference.local_models import assets, catalog, dependencies
from ...inference.local_models.llama_server import binary as llama_binary
from ..deps import _download_lock

logger = logging.getLogger(__name__)

router = APIRouter()


class _FeatureManagement(Protocol):
    """What a feature must offer to be managed from the generic Local ML card.

    Composition in the top layer, not a callback pointing down. The shared
    catalog describes artifacts; this describes behaviour, and putting these
    hooks on ``ModelSpec`` instead would turn the inference catalog into a
    registry of higher-layer behaviour — the dependency problem this refactor
    removed, in a less visible form.
    """

    async def status_extra(self, settings: Mapping[str, Any]) -> dict: ...

    async def sync_selection(self, *, prefer: str | None = None) -> dict: ...

    async def apply_config(self, body: Mapping[str, Any]) -> dict: ...

    async def on_enabled(self, enabled: bool) -> None: ...

    async def release_host(self) -> None: ...


#: The features that have management behaviour of their own. A feature absent
#: from this map is a plain download-and-toggle one, and the config route's 404
#: is exactly that statement.
_MANAGEMENT: dict[str, _FeatureManagement] = {prose_rewriter.FEATURE: prose_rewriter.integration}


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
    controller = _MANAGEMENT.get(feature)
    if controller is not None:
        return await controller.sync_selection(prefer=prefer)
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
        controller = _MANAGEMENT.get(f)
        if controller is not None:
            info.update(await controller.status_extra(settings))
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
    controller = _MANAGEMENT.get(feature)
    if controller is not None:
        # Before the unlink, not after — the feature explains why.
        await controller.release_host()
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
    controller = _MANAGEMENT.get(feature)
    if controller is None:
        raise HTTPException(status_code=404, detail=f"{feature!r} has no configurable variants")
    try:
        config = await controller.apply_config(data)
    except prose_rewriter.UnknownVariant as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except prose_rewriter.UnsupportedBatchSize as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"local_ml_config": config}


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
    controller = _MANAGEMENT.get(feature)
    if controller is not None:
        # Switching a feature on means "make this work", so the slice gets to
        # repair a selection that points at nothing, and pre-warm what it picks.
        await controller.on_enabled(enabled)
    settings = await get_settings()
    return {
        "local_ml_enabled": settings.get("local_ml_enabled", {}),
        "local_ml_config": settings.get("local_ml_config", {}),
    }
