"""Prose-rewriter runtime download route."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException

from ...features import prose_rewriter
from ...inference.local_models.llama_server import LlamaServerMissing
from ..deps import _download_lock

logger = logging.getLogger(__name__)

router = APIRouter()


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
        except LlamaServerMissing as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None
        except Exception:
            logger.exception("llama-server fetch (%s) failed", backend)
            raise HTTPException(status_code=500, detail="Runtime download failed; see server logs") from None
    return {"ok": True, "path": path}
