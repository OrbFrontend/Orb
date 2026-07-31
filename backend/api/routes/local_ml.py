"""Local-ML scaffold routes: status, one-at-a-time model download, and the
per-feature enable toggle. Drives the Settings "Local ML" tri-state card."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, HTTPException

from ...database import get_settings, set_local_ml_enabled
from ...inference import local_ml

logger = logging.getLogger(__name__)

router = APIRouter()

_download_lock = asyncio.Lock()


@router.get("/api/local-ml/status")
async def api_local_ml_status():
    """Per-feature tri-state: extras installed? model present? feature enabled?"""
    ok, reason = local_ml.deps_ok()
    settings = await get_settings()
    enabled_map = settings.get("local_ml_enabled", {})
    return {
        "deps_ok": ok,
        "reason": reason,
        "install_cmd": local_ml.install_cmd(),
        "features": {
            f: {"present": local_ml.present(f), "enabled": enabled_map.get(f, True), "size_mb": spec.size_mb}
            for f, spec in local_ml.MODELS.items()
        },
    }


@router.post("/api/local-ml/{feature}/download")
async def api_local_ml_download(feature: str):
    """Download feature's GGUF into backend/data/models/ (one at a time)."""
    if feature not in local_ml.MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown local-ML feature {feature!r}")
    ok, reason = local_ml.deps_ok()
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    async with _download_lock:
        try:
            await asyncio.to_thread(local_ml.download, feature)
        except Exception:
            logger.exception("local-ml download %r failed", feature)
            raise HTTPException(status_code=500, detail="Download failed; see server logs") from None
    return {"ok": True, "present": local_ml.present(feature)}


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
    if feature not in local_ml.MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown local-ML feature {feature!r}")
    await set_local_ml_enabled(feature, bool(data.get("enabled")))
    settings = await get_settings()
    return {"local_ml_enabled": settings.get("local_ml_enabled", {})}
