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

# ponytail: one global lock — a download is a big, rare, one-off; no need for
# per-feature locks. Serializes concurrent /download clicks.
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
        "features": {f: {"present": local_ml.present(f), "enabled": enabled_map.get(f, True)} for f in local_ml.MODELS},
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


@router.post("/api/local-ml/{feature}/enabled")
async def api_local_ml_enabled(feature: str, data: dict = Body(...)):  # noqa: B008
    """Flip one feature's on/off toggle; return the full decoded map."""
    if feature not in local_ml.MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown local-ML feature {feature!r}")
    await set_local_ml_enabled(feature, bool(data.get("enabled")))
    settings = await get_settings()
    return {"local_ml_enabled": settings.get("local_ml_enabled", {})}
