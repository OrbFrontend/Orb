"""Document CRUD and continuation routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ...core import scrub_log
from ...database import (
    create_document,
    delete_document,
    get_document,
    get_documents,
    get_phrase_bank,
    get_settings,
    update_document,
)
from ...features.documents import DocumentContinuer, audit_document, patch_document
from ...inference import AbortToken, client_from_settings
from ..deps import _active_aborts, _CleanupStreamingResponse, _sse_stream
from ..schemas import (
    DocumentAuditRequest,
    DocumentAuditResponse,
    DocumentCreate,
    DocumentGenerateRequest,
    DocumentPatchResponse,
    DocumentUpdate,
)

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
    client = client_from_settings(settings, abort_token=abort_token)
    continuer = DocumentContinuer(client, settings)

    async def _gen():
        try:
            finish = ""
            async for chunk in continuer.stream(
                data.prompt,
                settings.get("model_name", ""),
                assisted=data.assisted,
                token_probs=data.token_probs,
            ):
                if chunk["type"] == "content":
                    # Byte-identical wire: plain string, \n-escaped by _sse_stream.
                    yield {"event": "token", "data": chunk["delta"]}
                elif chunk["type"] == "token_probs":
                    # dict data auto-JSON-serialized by _sse_stream
                    yield {
                        "event": "probs",
                        "data": {"token": chunk["token"], "prob": chunk["prob"], "top": chunk["top"]},
                    }
                else:  # done — carries the transport's finish_reason
                    finish = chunk.get("finish_reason") or ""
            # Like `probs`, the done payload is a JSON dict the client must not
            # unescapeSSE. "length" marks a token-budget cutoff (Output Auditor
            # trims the dangling half-sentence before scanning).
            yield {"event": "done", "data": {"finish": finish}}
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


@router.post("/api/documents/{did}/audit")
async def api_audit_document(did: str, data: DocumentAuditRequest) -> DocumentAuditResponse:
    """Run the Output Auditor's prose scanners on a generated run (no LLM).

    Stateless like /generate: the client sends the run ("draft") and the
    preceding document text ("context"); nothing is persisted. 404s an unknown
    ``did`` first, same guard as generate.
    """
    if not await get_document(did):
        raise HTTPException(status_code=404, detail="Document not found")
    settings = await get_settings()
    phrase_bank = await get_phrase_bank()
    result = await audit_document(
        data.draft,
        data.context,
        phrase_bank,
        settings.get("document_audit_toggles"),
        assisted=data.assisted,
        truncated=data.truncated,
    )
    return DocumentAuditResponse(**result)


@router.post("/api/documents/{did}/patch")
async def api_patch_document(did: str, data: DocumentAuditRequest) -> DocumentPatchResponse:
    """Fix the run's audit findings with one forced editor_apply_patch call.

    Plain JSON (no SSE — patch output is short). The writer endpoint serves
    the call, consistent with doc mode hiding all Agent config.
    """
    if not await get_document(did):
        raise HTTPException(status_code=404, detail="Document not found")
    settings = await get_settings()
    phrase_bank = await get_phrase_bank()
    client = client_from_settings(settings)
    result = await patch_document(
        client,
        settings.get("model_name", ""),
        data.draft,
        data.context,
        phrase_bank,
        settings.get("document_audit_toggles"),
        settings,
        assisted=data.assisted,
        truncated=data.truncated,
    )
    return DocumentPatchResponse(**result)
