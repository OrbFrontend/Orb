"""Document-mode routes: CRUD + a stateless LLM continuation proxy (SSE).

HTTP concerns only — prompt shape and transport policy live in the
``features/documents`` slice. The generate route is a stateless proxy: the
client owns persistence of generated text (it still 404s an unknown ``did``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ...core import scrub_log
from ...database import (
    create_document,
    delete_document,
    get_document,
    get_documents,
    get_settings,
    update_document,
)
from ...features.documents import DocumentContinuer
from ...inference import AbortToken, LLMClient
from ..deps import _active_aborts, _CleanupStreamingResponse, _sse_stream
from ..schemas import DocumentCreate, DocumentGenerateRequest, DocumentUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/documents")
async def api_list_documents():
    return await get_documents()


@router.post("/api/documents")
async def api_create_document(data: DocumentCreate):
    return await create_document(data.model_dump(exclude_unset=True))


@router.get("/api/documents/{did}")
async def api_get_document(did: str):
    doc = await get_document(did)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.put("/api/documents/{did}")
async def api_update_document(did: str, data: DocumentUpdate):
    if not await get_document(did):
        raise HTTPException(status_code=404, detail="Document not found")
    return await update_document(did, data.model_dump(exclude_unset=True))


@router.delete("/api/documents/{did}")
async def api_delete_document(did: str):
    if not await delete_document(did):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"ok": True}


@router.post("/api/documents/{did}/generate")
async def api_generate_document(did: str, data: DocumentGenerateRequest, request: Request):
    """Stream a continuation of the document prefix from the cursor (SSE).

    Stateless proxy — the client persists generated text; this only reads
    settings and drives the LLM. 404s an unknown ``did`` first so garbage ids
    never mint locks/abort entries.
    """
    if not await get_document(did):
        raise HTTPException(status_code=404, detail="Document not found")

    settings = await get_settings()
    abort_token = AbortToken()
    client = LLMClient(
        settings["endpoint_url"],
        api_key=settings.get("api_key", ""),
        abort_token=abort_token,
        completion_mode=settings.get("completion_mode", "chat"),
    )
    continuer = DocumentContinuer(client, settings)

    async def _gen():
        try:
            async for chunk in continuer.stream(
                data.prompt,
                settings.get("model_name", ""),
                assisted=data.assisted,
                token_probs=data.token_probs,
            ):
                if chunk["type"] == "content":
                    # Byte-identical wire: plain string, \n-escaped by _sse_stream.
                    yield {"event": "token", "data": chunk["delta"]}
                else:  # token_probs — dict data auto-JSON-serialized by _sse_stream
                    yield {
                        "event": "probs",
                        "data": {"token": chunk["token"], "prob": chunk["prob"], "top": chunk["top"]},
                    }
            yield {"event": "done", "data": ""}
        except Exception as e:
            logger.error("Document generate error: %s", e)
            yield {"event": "error", "data": "Generation failed; see server logs"}

    return _CleanupStreamingResponse(
        _sse_stream(_gen(), request, abort_token=abort_token, cid=f"doc:{did}"),
        media_type="text/event-stream",
    )


@router.post("/api/documents/{did}/stop")
async def api_stop_document(did: str):
    """Abort the active continuation for this document, if any."""
    token = _active_aborts.get(f"doc:{did}")
    if token is not None:
        token.abort()
        logger.info("Stop requested for document %s — abort signalled", scrub_log(did))
    return {"ok": True}
