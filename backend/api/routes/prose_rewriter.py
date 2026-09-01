"""Prose-rewriter runtime download route."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ...features import prose_rewriter
from ...inference.local_models.llama_server import LlamaServerMissing
from ..deps import _download_lock

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/local-ml/prose_rewriter/runtime")
async def api_prose_rewriter_runtime():
    """Fetch the prebuilt llama-servers into backend/data/llama-bin/.

    NO FLAVOUR TO PICK: both the GPU and CPU builds are installed, and the
    feature's GPU setting then chooses between them per launch. Asking here
    instead is what made that setting inert — it wrote a flag onto whichever
    single build happened to be unpacked. This downloads and then executes
    native binaries from the official ggml-org release feed;
    ``ORB_LLAMA_SERVER`` is the escape hatch for a self-supplied one.
    """
    async with _download_lock:
        try:
            path = await prose_rewriter.integration.fetch_runtime()
        except LlamaServerMissing as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None
        except Exception:
            logger.exception("llama-server fetch failed")
            raise HTTPException(status_code=500, detail="Runtime download failed; see server logs") from None
    return {"ok": True, "path": path}
