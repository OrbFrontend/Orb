"""Document CRUD, continuation, audit, and prose-rewrite routes."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request

from ...core import scrub_log
from ...database import (
    create_document,
    get_document,
    get_documents,
    get_phrase_bank,
    get_settings,
    update_document,
)
from ...database.queries.documents import DocumentConflict
from ...features.documents import DocumentContinuer, audit_document, patch_document, scaffold_keeper, split_margins
from ...inference import AbortToken, client_from_settings, until_aborted
from ...workflows.contracts import PublicEvent
from ...workflows.prose_rewriter_host import ProseRewriteConfig, resolve_document_config, rewrite_events
from ..deletions import delete_document_settled
from ..deps import CleanupStreamingResponse, sse_stream, stop_active_stream
from ..schemas import (
    DocumentAuditRequest,
    DocumentAuditResponse,
    DocumentCreate,
    DocumentGenerateRequest,
    DocumentPatchResponse,
    DocumentRewriteRequest,
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
    try:
        return await update_document(did, data.model_dump(exclude_unset=True))
    except DocumentConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "document": exc.document}) from exc


@router.delete("/api/documents/{did}")
async def api_delete_document(did: str):
    if not await delete_document_settled(did):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"ok": True}


@router.post("/api/documents/{did}/generate")
async def api_generate_document(did: str, data: DocumentGenerateRequest, request: Request):
    """Stream a continuation of the document prefix from the cursor (SSE).

    Stateless proxy -- the client persists generated text; this only reads settings and drives the LLM. 404s an unknown ``did``
    first so garbage ids never mint locks/abort entries.
    """
    if not await get_document(did):
        raise HTTPException(status_code=404, detail="Document not found")

    settings = await get_settings()
    abort_token = AbortToken()
    client = client_from_settings(settings, abort_token=abort_token)
    continuer = DocumentContinuer(client, settings)

    async def _gen() -> AsyncIterator[PublicEvent]:
        finish = ""
        async for chunk in continuer.stream(
            data.prompt, settings.get("model_name", ""), assisted=data.assisted, token_probs=data.token_probs
        ):
            if chunk["type"] == "content":
                # Byte-identical wire: plain string, \n-escaped by sse_stream.
                yield {"event": "token", "data": chunk["delta"]}
            elif chunk["type"] == "token_probs":
                # dict data auto-JSON-serialized by sse_stream
                yield {"event": "probs", "data": {"token": chunk["token"], "prob": chunk["prob"], "top": chunk["top"]}}
            else:  # done -- carries the transport's finish_reason
                finish = chunk.get("finish_reason") or ""
        # Like `probs`, the done payload is a JSON dict the client must not unescapeSSE. "length" marks a token-budget
        # cutoff (Output Auditor trims the dangling half-sentence before scanning).
        yield {"event": "done", "data": {"finish": finish}}

    return CleanupStreamingResponse(
        sse_stream(_gen(), request, abort_token=abort_token, cid=f"doc:{did}"), media_type="text/event-stream"
    )


@router.post("/api/documents/{did}/stop")
async def api_stop_document(did: str, operation_id: str | None = None):
    """Abort the active continuation for this document, if any, and wait for it to settle."""
    result = await stop_active_stream(f"doc:{did}", operation_id=operation_id)
    if result["active"]:
        logger.info("Stop requested for document %s — abort signalled", scrub_log(did))
    return {"ok": True, **result}


@router.post("/api/documents/{did}/audit")
async def api_audit_document(did: str, data: DocumentAuditRequest) -> DocumentAuditResponse:
    """Run the Output Auditor's prose scanners on a generated run (no LLM).

    Stateless like /generate: the client sends the run ("draft") and the preceding document text ("context"); nothing is
    persisted. 404s an unknown ``did`` first, same guard as generate.
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

    Plain JSON (no SSE -- patch output is short). The writer endpoint serves
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


async def _stream_prose_rewrite(
    text: str, assisted: bool, config: ProseRewriteConfig, abort_token: AbortToken
) -> AsyncIterator[PublicEvent]:
    """Stream the selection's rewrite with its margins kept; the client splices and saves the result."""
    lead, core, trail = split_margins(text)
    rewritten = core
    warning = ""
    events = until_aborted(rewrite_events(core, config, scaffold_keeper(assisted)), abort_token)
    async with contextlib.aclosing(events) as stream:
        async for event in stream:
            if event["type"] == "draft_update":
                yield {"event": "rewrite_update", "data": {"text": lead + event["draft"] + trail}}
            elif event["type"] == "rewritten":
                rewritten = event["draft"]
            elif event["type"] == "warning":
                warning = event["reason"]
    if abort_token.is_aborted:
        return
    result = text if warning else lead + rewritten + trail
    yield {"event": "done", "data": {"text": result, "changed": result != text, "warning": warning}}


@router.post("/api/documents/{did}/prose-rewrite")
async def api_prose_rewrite_document(did: str, data: DocumentRewriteRequest, request: Request):
    """Stream a local prose rewrite of selected document text (SSE).

    Stateless like /generate and shares its stream lock and /stop; nothing is persisted.
    """
    if not await get_document(did):
        raise HTTPException(status_code=404, detail="Document not found")
    if not data.text.strip():
        raise HTTPException(status_code=409, detail="Select text to rewrite")
    config = resolve_document_config(await get_settings())
    if config is None:
        raise HTTPException(status_code=503, detail="Prose rewriter unavailable: turn it on and pick a model in Workflow")
    abort_token = AbortToken()
    return CleanupStreamingResponse(
        sse_stream(
            _stream_prose_rewrite(data.text, data.assisted, config, abort_token),
            request,
            abort_token=abort_token,
            cid=f"doc:{did}",
        ),
        media_type="text/event-stream",
    )
